"""NFL schedule and bye weeks, from the nflverse schedules release.

Bye weeks are the backbone of the pressure analysis: a manager whose only two
starting running backs are off in the same week has to act, whether he has
noticed yet or not. They are derived rather than fetched - a team's bye is the
regular-season week it does not appear in `schedules/games.csv`.

The schedule is fixed once the season is announced, so it is cached for a week.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

import httpx

from app.cache import KeyedDiskCache
from app.config import get_settings
from app.nflverse import _download_and_parse
from app.teams import normalise_abbr

log = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = 1
SCHEDULE_TTL_SECONDS = 7 * 24 * 3600


class ScheduleProvider:
    def __init__(self, client: httpx.AsyncClient) -> None:
        settings = get_settings()
        self._client = client
        self._settings = settings
        self._base_url = settings.nflverse_base_url.rstrip("/")
        self.cache = KeyedDiskCache(
            settings.cache_path("schedule_cache.json"),
            name="schedule",
            default_ttl_seconds=SCHEDULE_TTL_SECONDS,
            schema_version=CACHE_SCHEMA_VERSION,
        )

    async def season(self, season: int) -> tuple[dict[str, Any], dict[str, Any]]:
        return await self.cache.get_or_refresh(
            f"season:{season}", lambda: self._build(season)
        )

    async def _build(self, season: int) -> dict[str, Any]:
        parsed = await _download_and_parse(
            self._client,
            self._base_url,
            "schedules",
            "games.csv",
            lambda path: _parse_schedule(path, season),
            self._settings.nflverse_download_timeout,
        )
        return parsed or {"season": season, "byes": {}, "games": [], "weeks": []}

    async def byes(self, season: int) -> dict[str, int | None]:
        """team abbreviation -> its bye week."""
        data, _ = await self.season(season)
        return data.get("byes") or {}


def _parse_schedule(path: Path, season: int) -> dict[str, Any]:
    weeks: set[int] = set()
    playing: dict[str, set[int]] = {}
    games: list[dict[str, Any]] = []

    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("season") != str(season) or row.get("game_type") != "REG":
                continue
            try:
                week = int(row["week"])
            except (KeyError, TypeError, ValueError):
                continue
            weeks.add(week)

            home = normalise_abbr(row.get("home_team"))
            away = normalise_abbr(row.get("away_team"))
            for team in (home, away):
                if team:
                    playing.setdefault(team, set()).add(week)

            games.append(
                {
                    "game_id": row.get("game_id"),
                    "week": week,
                    "gameday": row.get("gameday"),
                    "weekday": row.get("weekday"),
                    "gametime": row.get("gametime"),
                    "home_team": home,
                    "away_team": away,
                }
            )

    # A team's bye is the regular-season week it simply does not appear in.
    byes: dict[str, int | None] = {}
    for team, played in playing.items():
        missing = sorted(weeks - played)
        byes[team] = missing[0] if missing else None

    return {
        "season": season,
        "weeks": sorted(weeks),
        "byes": byes,
        "games": games,
        "teams": len(playing),
    }
