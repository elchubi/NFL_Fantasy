"""Disk-backed cache for Sleeper's NFL player file.

The file is ~5MB and Sleeper asks integrators not to pull it more than once a
day, so it is stored on disk with a fetch timestamp and only refreshed when it
goes stale (PLAYERS_CACHE_TTL_HOURS, 20h by default). Only the fields we
actually surface are kept, which cuts the on-disk file to a fraction of the
original.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.config import get_settings
from app.sleeper import SleeperClient

log = logging.getLogger(__name__)

# Bumped whenever _KEEP_FIELDS changes, so an older cache file on disk is
# refetched instead of being served without the newer fields.
CACHE_SCHEMA_VERSION = 2

# Fields worth keeping from each Sleeper player record.
_KEEP_FIELDS = (
    # Cross-source join keys: gsis_id reaches nflverse, espn_id reaches ESPN.
    "gsis_id",
    "espn_id",
    "first_name",
    "last_name",
    "full_name",
    "position",
    "fantasy_positions",
    "team",
    "status",
    "injury_status",
    "injury_body_part",
    "injury_notes",
    "number",
    "age",
    "years_exp",
    "depth_chart_order",
    "depth_chart_position",
)


def _slim(player_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    slim = {k: raw.get(k) for k in _KEEP_FIELDS if raw.get(k) is not None}
    if not slim.get("full_name"):
        name = " ".join(
            part for part in (raw.get("first_name"), raw.get("last_name")) if part
        ).strip()
        # Team defenses come keyed by team abbreviation with no name fields.
        slim["full_name"] = name or player_id
    return slim


class PlayerStore:
    """Loads, caches and serves the player dictionary."""

    def __init__(self, client: SleeperClient) -> None:
        settings = get_settings()
        self._client = client
        self._path = Path(settings.players_cache_path)
        self._ttl_seconds = settings.players_cache_ttl_hours * 3600
        self._players: dict[str, dict[str, Any]] = {}
        self._by_gsis: dict[str, str] = {}
        self._by_espn: dict[str, str] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    def _reindex(self) -> None:
        """Index Sleeper ids by the ids the other data sources use."""
        self._by_gsis = {}
        self._by_espn = {}
        for pid, raw in self._players.items():
            gsis = raw.get("gsis_id")
            if gsis:
                self._by_gsis[str(gsis)] = pid
            espn = raw.get("espn_id")
            if espn:
                self._by_espn[str(espn)] = pid

    # --- Cache state ----------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return bool(self._players)

    @property
    def fetched_at(self) -> float:
        return self._fetched_at

    @property
    def is_stale(self) -> bool:
        return (time.time() - self._fetched_at) > self._ttl_seconds

    @property
    def count(self) -> int:
        return len(self._players)

    def status(self) -> dict[str, Any]:
        return {
            "players_cached": self.count,
            "cache_path": str(self._path),
            "fetched_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._fetched_at))
                if self._fetched_at
                else None
            ),
            "age_hours": (
                round((time.time() - self._fetched_at) / 3600, 2)
                if self._fetched_at
                else None
            ),
            "stale": self.is_stale,
        }

    # --- Loading --------------------------------------------------------------

    def load_from_disk(self) -> bool:
        """Populate the in-memory copy from disk. Returns True on success."""
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
                log.info(
                    "Player cache schema changed (%s -> %s); refetching.",
                    payload.get("schema_version"),
                    CACHE_SCHEMA_VERSION,
                )
                return False
            players = payload["players"]
            if not isinstance(players, dict) or not players:
                raise ValueError("empty player map")
            self._players = players
            self._fetched_at = float(payload.get("fetched_at") or 0.0)
            self._reindex()
        except FileNotFoundError:
            log.info("No player cache at %s yet.", self._path)
            return False
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Ignoring unreadable player cache %s: %s", self._path, exc)
            return False
        log.info(
            "Loaded %s players from cache (%.1fh old).",
            len(self._players),
            (time.time() - self._fetched_at) / 3600,
        )
        return True

    def _save_to_disk(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "schema_version": CACHE_SCHEMA_VERSION,
                        "fetched_at": self._fetched_at,
                        "players": self._players,
                    },
                    fh,
                )
            tmp.replace(self._path)
        except OSError as exc:  # A read-only volume shouldn't take the API down.
            log.warning("Could not write player cache to %s: %s", self._path, exc)

    async def ensure_fresh(self) -> None:
        """Refresh from Sleeper if the cache is missing or older than the TTL."""
        if self.loaded and not self.is_stale:
            return

        async with self._lock:
            # Another request may have refreshed while we waited for the lock.
            if self.loaded and not self.is_stale:
                return
            if not self.loaded and self.load_from_disk() and not self.is_stale:
                return

            log.info("Refreshing the Sleeper player file...")
            try:
                raw = await self._client.all_players()
            except HTTPException as exc:
                if self.loaded:
                    # Serving slightly stale names beats serving an error.
                    log.warning(
                        "Player refresh failed (%s); serving the cached copy.",
                        exc.detail,
                    )
                    return
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "The Sleeper player file is unavailable and nothing is "
                        f"cached yet: {exc.detail}"
                    ),
                ) from exc

            self._players = {
                pid: _slim(pid, data)
                for pid, data in raw.items()
                if isinstance(data, dict)
            }
            self._fetched_at = time.time()
            self._reindex()
            self._save_to_disk()
            log.info("Cached %s players.", len(self._players))

    # --- Resolution -----------------------------------------------------------

    def resolve(self, player_id: Any) -> dict[str, Any]:
        """Turn a raw Sleeper player id into a readable player record."""
        pid = str(player_id)
        raw = self._players.get(pid)
        if raw is None:
            return {
                "player_id": pid,
                "name": f"Unknown player ({pid})",
                "position": None,
                "nfl_team": None,
                "status": None,
                "injury_status": None,
                "resolved": False,
            }

        player = {
            "player_id": pid,
            "name": raw.get("full_name") or pid,
            "position": raw.get("position"),
            "nfl_team": raw.get("team"),
            "status": raw.get("status"),
            "injury_status": raw.get("injury_status"),
            "resolved": True,
        }
        if raw.get("injury_body_part"):
            player["injury_body_part"] = raw["injury_body_part"]
        if raw.get("injury_notes"):
            player["injury_notes"] = raw["injury_notes"]
        for optional in ("number", "age", "years_exp", "fantasy_positions"):
            if raw.get(optional) is not None:
                player[optional] = raw[optional]
        return player

    def resolve_many(self, player_ids: Any) -> list[dict[str, Any]]:
        if not player_ids:
            return []
        return [self.resolve(pid) for pid in player_ids if pid not in (None, "0", 0)]

    # --- Cross-source ids -----------------------------------------------------

    def raw(self, player_id: Any) -> dict[str, Any] | None:
        return self._players.get(str(player_id))

    def gsis_id(self, player_id: Any) -> str | None:
        """nflverse keys everything by gsis_id; Sleeper carries it per player."""
        raw = self._players.get(str(player_id))
        return str(raw["gsis_id"]) if raw and raw.get("gsis_id") else None

    def espn_id(self, player_id: Any) -> str | None:
        raw = self._players.get(str(player_id))
        return str(raw["espn_id"]) if raw and raw.get("espn_id") else None

    def sleeper_id_for_gsis(self, gsis_id: str) -> str | None:
        return self._by_gsis.get(str(gsis_id))

    def sleeper_id_for_espn(self, espn_id: Any) -> str | None:
        return self._by_espn.get(str(espn_id))

    def find_by_name(self, name: str, team: str | None = None) -> str | None:
        """Last-resort lookup when a source gives no usable id, only a name."""
        needle = " ".join(str(name).lower().split())
        if not needle:
            return None
        fallback = None
        for pid, raw in self._players.items():
            if " ".join(str(raw.get("full_name", "")).lower().split()) != needle:
                continue
            if team and raw.get("team") and str(raw["team"]).upper() == str(team).upper():
                return pid
            fallback = fallback or pid
        return fallback
