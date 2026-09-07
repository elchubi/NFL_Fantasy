"""Disk-backed JSON caches with TTLs.

Each external source has its own cache file and its own refresh cadence:

    players_cache.json    Sleeper player file        20h
    nflverse_cache.json   nflverse weekly releases   24h
    odds_cache.json       The Odds API               24h (protects the free quota)
    injuries_cache.json   ESPN injury reports        3h  (changes during the week)
    weather_cache.json    Open-Meteo forecasts       12h, 1h on game day

Entries are keyed (by week, team, season, ...) inside a single file per source,
each with its own timestamp, so one stale key never forces a full refresh.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class KeyedDiskCache:
    """A `{key: {fetched_at, data}}` map persisted as one JSON file.

    `schema_version` guards against serving a cache written by an older build
    with a different shape: a mismatch is treated as an empty cache.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        name: str,
        default_ttl_seconds: float,
        schema_version: int = 1,
    ) -> None:
        self.name = name
        self.path = Path(path)
        self.default_ttl = default_ttl_seconds
        self.schema_version = schema_version
        self._entries: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._loaded = False

    # --- Persistence ----------------------------------------------------------

    def load(self) -> bool:
        """Read the cache file into memory. Safe to call more than once."""
        if self._loaded:
            return bool(self._entries)
        self._loaded = True
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            log.info("[%s] no cache file at %s yet.", self.name, self.path)
            return False
        except (OSError, ValueError) as exc:
            log.warning("[%s] ignoring unreadable cache %s: %s", self.name, self.path, exc)
            return False

        if payload.get("schema_version") != self.schema_version:
            log.info(
                "[%s] cache schema changed (%s -> %s); refetching.",
                self.name,
                payload.get("schema_version"),
                self.schema_version,
            )
            return False

        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return False
        self._entries = entries
        log.info("[%s] loaded %s cached entries.", self.name, len(entries))
        return True

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(
                    {"schema_version": self.schema_version, "entries": self._entries},
                    fh,
                )
            tmp.replace(self.path)
        except OSError as exc:  # A read-only volume shouldn't take the API down.
            log.warning("[%s] could not write cache to %s: %s", self.name, self.path, exc)

    # --- Entries --------------------------------------------------------------

    def peek(self, key: str) -> dict[str, Any] | None:
        self.load()
        return self._entries.get(key)

    def age_seconds(self, key: str) -> float | None:
        entry = self.peek(key)
        if not entry:
            return None
        return time.time() - float(entry.get("fetched_at") or 0)

    def is_stale(self, key: str, ttl: float | None = None) -> bool:
        age = self.age_seconds(key)
        if age is None:
            return True
        return age > (self.default_ttl if ttl is None else ttl)

    def put(self, key: str, data: Any) -> None:
        self.load()
        self._entries[key] = {"fetched_at": time.time(), "data": data}
        self.save()

    def meta(self, key: str) -> dict[str, Any]:
        entry = self.peek(key)
        if not entry:
            return {"source_cached": False, "fetched_at": None, "age_hours": None}
        fetched_at = float(entry.get("fetched_at") or 0)
        return {
            "source_cached": True,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(fetched_at)),
            "age_hours": round((time.time() - fetched_at) / 3600, 2),
            "stale": self.is_stale(key),
        }

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def get_or_refresh(
        self,
        key: str,
        loader: Callable[[], Awaitable[Any]],
        *,
        ttl: float | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Return cached data for `key`, refreshing it through `loader` if stale.

        Concurrent callers for the same key share one refresh. If the refresh
        fails but stale data exists, the stale copy is served with a warning
        rather than failing the request.
        """
        if not self.is_stale(key, ttl):
            return self.peek(key)["data"], self.meta(key)

        async with self._lock(key):
            # Another request may have refreshed while we waited for the lock.
            if not self.is_stale(key, ttl):
                return self.peek(key)["data"], self.meta(key)

            log.info("[%s] refreshing '%s'...", self.name, key)
            try:
                data = await loader()
            except Exception as exc:  # noqa: BLE001 - stale beats nothing
                stale = self.peek(key)
                if stale is not None:
                    log.warning(
                        "[%s] refresh of '%s' failed (%s); serving the stale copy.",
                        self.name,
                        key,
                        exc,
                    )
                    meta = self.meta(key)
                    meta["refresh_failed"] = str(getattr(exc, "detail", exc))
                    return stale["data"], meta
                raise

            self.put(key, data)
            return data, self.meta(key)

    def status(self) -> dict[str, Any]:
        self.load()
        return {
            "cache_path": str(self.path),
            "entries": {key: self.meta(key) for key in sorted(self._entries)},
        }
