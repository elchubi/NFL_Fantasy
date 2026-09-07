"""Advanced player stats from the nflverse-data GitHub releases.

Three CSV releases are combined into one per-player view, keyed by `gsis_id`
(the field Sleeper also carries on every player):

    stats_player/stats_player_week_<season>.csv   volume, target share,
                                                  air yards, EPA, fantasy points
    snap_counts/snap_counts_<season>.csv          snap counts and snap %
    pbp/play_by_play_<season>.csv                 red zone touches (optional)

snap_counts is keyed by Pro-Football-Reference id rather than gsis_id, so
`players/players.csv` is pulled as the id crosswalk between the two.

The play-by-play file is ~98MB and is the only place red zone usage exists;
it is streamed to disk, aggregated in one pass (a couple of seconds, no pandas)
and then deleted. Set NFLVERSE_INCLUDE_RED_ZONE=false to skip it.

Everything is refreshed at most once every NFLVERSE_CACHE_TTL_HOURS (24h by
default), which matches how often nflverse publishes.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import tempfile
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

from app.cache import KeyedDiskCache
from app.config import get_settings
from app.http import download_to_file

log = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = 1

# Weekly columns worth keeping, mapped to the names we serve.
WEEKLY_FIELDS: dict[str, str] = {
    "team": "team",
    "opponent_team": "opponent",
    "position": "position",
    # Receiving
    "targets": "targets",
    "receptions": "receptions",
    "receiving_yards": "receiving_yards",
    "receiving_tds": "receiving_tds",
    "receiving_air_yards": "air_yards",
    "target_share": "target_share",
    "air_yards_share": "air_yards_share",
    "wopr": "wopr",
    "racr": "racr",
    "receiving_epa": "receiving_epa",
    # Rushing
    "carries": "carries",
    "rushing_yards": "rushing_yards",
    "rushing_tds": "rushing_tds",
    "rushing_epa": "rushing_epa",
    # Passing
    "attempts": "pass_attempts",
    "completions": "completions",
    "passing_yards": "passing_yards",
    "passing_tds": "passing_tds",
    "passing_air_yards": "passing_air_yards",
    "passing_epa": "passing_epa",
    # Fantasy
    "fantasy_points_ppr": "fantasy_points_ppr",
}

NUMERIC_FIELDS = {v for k, v in WEEKLY_FIELDS.items() if k not in ("team", "opponent_team", "position")}

# Metrics averaged when comparing recent form against the season.
TREND_FIELDS = ("snap_pct", "target_share", "targets", "carries", "red_zone_touches")

RECENT_WEEKS = 3


async def _download_and_parse(
    client: httpx.AsyncClient,
    base_url: str,
    release: str,
    filename: str,
    parser: Callable[[Path], Any],
    timeout: float,
    *,
    allow_missing: bool = True,
) -> Any:
    """Download an nflverse release asset and parse it off the event loop.

    The file lives in a temp dir for the duration of the parse and is deleted
    afterwards; only the aggregate is kept. Parsing is CPU bound (a few seconds
    for play-by-play), so it runs in a worker thread.
    """
    with tempfile.TemporaryDirectory(prefix="nflverse-") as tmpdir:
        destination = Path(tmpdir) / filename
        ok = await download_to_file(
            client,
            f"{base_url.rstrip('/')}/{release}/{filename}",
            destination,
            source="nflverse",
            timeout=timeout,
            allow_404=allow_missing,
        )
        if not ok:
            return None
        log.info(
            "nflverse: downloaded %s (%.1f MB)",
            filename,
            destination.stat().st_size / 1_048_576,
        )
        return await asyncio.to_thread(parser, destination)


def _num(value: Any) -> float | None:
    if value in (None, "", "NA", "NaN", "nan"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: float | None, places: int = 3) -> float | None:
    return None if value is None else round(value, places)


class NflverseProvider:
    """Downloads, aggregates and serves the nflverse weekly releases."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        settings = get_settings()
        self._client = client
        self._settings = settings
        self._base_url = settings.nflverse_base_url.rstrip("/")
        # Memo so the "is this season published yet?" fallback is decided once.
        self._resolved_season: dict[int, int] = {}
        self.cache = KeyedDiskCache(
            settings.cache_path("nflverse_cache.json"),
            name="nflverse",
            default_ttl_seconds=settings.nflverse_cache_ttl_hours * 3600,
            schema_version=CACHE_SCHEMA_VERSION,
        )

    # --- Season resolution ----------------------------------------------------

    async def season_data(self, season: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """Aggregated stats for `season`, falling back to the previous season.

        Right after a season rolls over nflverse has not published the new
        season's files yet, so an empty current season falls back rather than
        returning nothing.
        """
        requested = season
        season = self._resolved_season.get(requested, requested)

        data, meta = await self.cache.get_or_refresh(
            f"season:{season}", lambda: self._build_season(season)
        )
        if not data.get("players") and season > 2000:
            previous = season - 1
            log.info("nflverse has no %s data yet; falling back to %s.", season, previous)
            data, meta = await self.cache.get_or_refresh(
                f"season:{previous}", lambda: self._build_season(previous)
            )
            if data.get("players"):
                self._resolved_season[requested] = previous
        return data, meta

    # --- Aggregation ----------------------------------------------------------

    async def _build_season(self, season: int) -> dict[str, Any]:
        weekly = await self._load_weekly_stats(season)
        sources = ["stats_player_week"]

        if weekly:
            crosswalk = await self._load_pfr_crosswalk()
            snaps = await self._load_snap_counts(season, crosswalk)
            if snaps:
                sources.append("snap_counts")
                for (gsis, week), snap in snaps.items():
                    entry = weekly.get(gsis, {}).get("weeks", {}).get(week)
                    if entry is not None:
                        entry.update(snap)

            if self._settings.nflverse_include_red_zone:
                red_zone = await self._load_red_zone(season)
                if red_zone:
                    sources.append("play_by_play (red zone)")
                    for (gsis, week), rz in red_zone.items():
                        entry = weekly.get(gsis, {}).get("weeks", {}).get(week)
                        if entry is not None:
                            entry.update(rz)

        weeks_seen: set[int] = set()
        for player in weekly.values():
            weeks_seen.update(int(w) for w in player["weeks"])
            self._summarise(player)
            _compact(player)

        return {
            "season": season,
            "sources": sources,
            "red_zone_included": self._settings.nflverse_include_red_zone,
            "weeks_available": sorted(weeks_seen),
            "players": weekly,
        }

    def _summarise(self, player: dict[str, Any]) -> None:
        """Add season totals/averages and a recent-form trend to one player."""
        weeks = player["weeks"]
        ordered = [weeks[w] for w in sorted(weeks, key=int)]
        played = [w for w in ordered if (w.get("snap_pct") or 0) > 0 or w.get("targets") or w.get("carries")]

        def avg(rows: list[dict[str, Any]], field: str) -> float | None:
            values = [r[field] for r in rows if r.get(field) is not None]
            return _round(sum(values) / len(values)) if values else None

        def total(rows: list[dict[str, Any]], field: str) -> float | None:
            values = [r[field] for r in rows if r.get(field) is not None]
            return _round(sum(values), 2) if values else None

        recent = ordered[-RECENT_WEEKS:]
        player["games"] = len(ordered)
        player["season_totals"] = {
            f: total(ordered, f)
            for f in (
                "targets", "receptions", "receiving_yards", "receiving_tds", "air_yards",
                "carries", "rushing_yards", "rushing_tds",
                "pass_attempts", "passing_yards", "passing_tds",
                "red_zone_touches", "red_zone_tds", "fantasy_points_ppr",
            )
        }
        player["season_averages"] = {
            f: avg(ordered, f)
            for f in ("snap_pct", "target_share", "air_yards_share", "wopr",
                      "targets", "carries", "red_zone_touches", "fantasy_points_ppr")
        }
        player["recent_averages"] = {
            "weeks": [w["week"] for w in recent],
            **{f: avg(recent, f) for f in TREND_FIELDS},
        }

        # With no more games than the recent window, "recent" and "season" are
        # the same rows and the delta would always be zero. Reporting that as
        # a flat trend would read as "role is stable", which is not what it
        # means, so the trend is withheld until there is something to compare.
        if len(ordered) > RECENT_WEEKS:
            player["trend"] = {
                f: _round(
                    (player["recent_averages"].get(f) or 0)
                    - (player["season_averages"].get(f) or 0)
                )
                for f in TREND_FIELDS
                if player["recent_averages"].get(f) is not None
                and player["season_averages"].get(f) is not None
            }
            player["role_note"] = self._role_note(player)
        else:
            player["trend"] = {}
            player["trend_note"] = (
                f"Only {len(ordered)} game(s) played; a trend needs more than "
                f"{RECENT_WEEKS} to mean anything."
            )
            player["role_note"] = None
        # `played` is only used to decide whether the player has any usage at
        # all; keep the flag rather than the rows.
        player["has_usage"] = bool(played)

    @staticmethod
    def _role_note(player: dict[str, Any]) -> str | None:
        """A one-line read on whether the player's role is moving."""
        trend = player.get("trend") or {}
        snap = trend.get("snap_pct")
        share = trend.get("target_share")
        notes = []
        if snap is not None and abs(snap) >= 0.08:
            direction = "up" if snap > 0 else "down"
            notes.append(f"snap share trending {direction} {abs(snap) * 100:.0f} points vs season average")
        if share is not None and abs(share) >= 0.03:
            direction = "up" if share > 0 else "down"
            notes.append(f"target share trending {direction} {abs(share) * 100:.0f} points")
        rz = trend.get("red_zone_touches")
        if rz is not None and abs(rz) >= 1:
            direction = "more" if rz > 0 else "fewer"
            notes.append(f"{abs(rz):.1f} {direction} red zone touches per game recently")
        return "; ".join(notes) if notes else None

    # --- Individual files -----------------------------------------------------

    def _url(self, release: str, filename: str) -> str:
        return f"{self._base_url}/{release}/{filename}"

    async def _with_csv(
        self,
        release: str,
        filename: str,
        parser: Callable[[Path], Any],
        *,
        allow_missing: bool = True,
    ) -> Any:
        return await _download_and_parse(
            self._client,
            self._base_url,
            release,
            filename,
            parser,
            self._settings.nflverse_download_timeout,
            allow_missing=allow_missing,
        )

    async def _load_weekly_stats(self, season: int) -> dict[str, dict[str, Any]]:
        result = await self._with_csv(
            "stats_player", f"stats_player_week_{season}.csv", _parse_weekly_stats
        )
        return result or {}

    async def _load_pfr_crosswalk(self) -> dict[str, str]:
        """pfr_id -> gsis_id, so snap counts can be joined to everything else."""
        result = await self._with_csv("players", "players.csv", _parse_crosswalk)
        return result or {}

    async def _load_snap_counts(
        self, season: int, crosswalk: dict[str, str]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        if not crosswalk:
            return {}
        result = await self._with_csv(
            "snap_counts",
            f"snap_counts_{season}.csv",
            lambda path: _parse_snap_counts(path, crosswalk),
        )
        return result or {}

    async def _load_red_zone(self, season: int) -> dict[tuple[str, str], dict[str, Any]]:
        """Red zone touches per player-week, aggregated from play-by-play."""
        result = await self._with_csv(
            "pbp", f"play_by_play_{season}.csv", _parse_red_zone
        )
        return result or {}

    # --- Serving --------------------------------------------------------------

    async def for_gsis_id(self, gsis_id: str, season: int) -> dict[str, Any]:
        data, meta = await self.season_data(season)
        player = (data.get("players") or {}).get(str(gsis_id))
        return {
            "season": data.get("season"),
            "sources": data.get("sources"),
            "weeks_available": data.get("weeks_available"),
            "found": player is not None,
            "stats": self._present(player) if player else None,
            "cache": meta,
        }

    @staticmethod
    def _present(player: dict[str, Any]) -> dict[str, Any]:
        weeks = player.get("weeks") or {}
        return {
            "gsis_id": player.get("gsis_id"),
            "name": player.get("name"),
            "position": player.get("position"),
            "team": player.get("team"),
            "games": player.get("games"),
            "season_totals": player.get("season_totals"),
            "season_averages": player.get("season_averages"),
            "recent_averages": player.get("recent_averages"),
            "trend_vs_season": player.get("trend"),
            "role_note": player.get("role_note"),
            "by_week": [weeks[w] for w in sorted(weeks, key=int)],
        }


def require_gsis(players: Any, player_id: str) -> str:
    """Resolve a Sleeper player id to its gsis_id or explain why it can't be."""
    gsis = players.gsis_id(player_id)
    if gsis:
        return gsis
    if players.raw(player_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"'{player_id}' is not a known Sleeper player id.",
        )
    raise HTTPException(
        status_code=404,
        detail=(
            f"Sleeper has no gsis_id for player '{player_id}', so there is no way "
            "to join them to nflverse. This happens for team defenses and for "
            "players who have never appeared in an NFL game."
        ),
    )


# --- CSV parsers (sync; run in a worker thread) -------------------------------


def _parse_weekly_stats(path: Path) -> dict[str, dict[str, Any]]:
    """stats_player_week_<season>.csv -> {gsis_id: {..., weeks: {week: row}}}.

    `player_id` in this release is the gsis_id, which is the same id Sleeper
    carries, so no crosswalk is needed here.
    """
    players: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            gsis = row.get("player_id")
            week = row.get("week")
            if not gsis or not week:
                continue
            if row.get("season_type") not in (None, "", "REG", "POST"):
                continue

            player = players.setdefault(
                gsis,
                {
                    "gsis_id": gsis,
                    "name": row.get("player_display_name") or row.get("player_name"),
                    "position": row.get("position"),
                    "team": row.get("team"),
                    "weeks": {},
                },
            )
            player["team"] = row.get("team") or player["team"]

            entry: dict[str, Any] = {
                "week": int(week),
                "season_type": row.get("season_type") or None,
            }
            for source_field, name in WEEKLY_FIELDS.items():
                value = row.get(source_field)
                if name in NUMERIC_FIELDS:
                    entry[name] = _round(_num(value), 4)
                elif value not in (None, "", "NA"):
                    entry[name] = value
            player["weeks"][str(week)] = entry
    return players


def _parse_crosswalk(path: Path) -> dict[str, str]:
    """players.csv -> {pfr_id: gsis_id}."""
    crosswalk: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pfr, gsis = row.get("pfr_id"), row.get("gsis_id")
            if pfr and gsis and pfr != "NA" and gsis != "NA":
                crosswalk[pfr] = gsis
    return crosswalk


def _parse_snap_counts(
    path: Path, crosswalk: dict[str, str]
) -> dict[tuple[str, str], dict[str, Any]]:
    """snap_counts_<season>.csv, re-keyed from pfr_player_id onto gsis_id."""
    snaps: dict[tuple[str, str], dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            gsis = crosswalk.get(row.get("pfr_player_id") or "")
            week = row.get("week")
            if not gsis or not week:
                continue
            snaps[(gsis, str(week))] = {
                "offense_snaps": _num(row.get("offense_snaps")),
                "snap_pct": _round(_num(row.get("offense_pct")), 4),
                "st_snaps": _num(row.get("st_snaps")),
            }
    return snaps


def _parse_red_zone(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """play_by_play_<season>.csv -> red zone carries/targets/TDs per player-week.

    Red zone is `yardline_100 <= 20`. The file is wide (~380 columns) but only
    has ~50k rows, so a single streaming pass is cheap.
    """
    csv.field_size_limit(10_000_000)
    counts: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {
            "red_zone_carries": 0,
            "red_zone_targets": 0,
            "red_zone_receptions": 0,
            "red_zone_tds": 0,
        }
    )
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            yardline = _num(row.get("yardline_100"))
            if yardline is None or yardline > 20:
                continue
            week = row.get("week")
            if not week:
                continue
            touchdown = row.get("touchdown") == "1"

            rusher = row.get("rusher_player_id")
            if rusher and rusher != "NA":
                bucket = counts[(rusher, str(week))]
                bucket["red_zone_carries"] += 1
                if touchdown and row.get("rush_attempt") == "1":
                    bucket["red_zone_tds"] += 1

            receiver = row.get("receiver_player_id")
            if receiver and receiver != "NA":
                bucket = counts[(receiver, str(week))]
                bucket["red_zone_targets"] += 1
                if row.get("complete_pass") == "1":
                    bucket["red_zone_receptions"] += 1
                    if touchdown:
                        bucket["red_zone_tds"] += 1

    return {
        key: {**value, "red_zone_touches": value["red_zone_carries"] + value["red_zone_targets"]}
        for key, value in counts.items()
    }


def _compact(player: dict[str, Any]) -> None:
    """Drop null and zero fields in place.

    Halves the cached file (and the memory it occupies) without losing
    anything: a missing key means the stat is zero or was not recorded.
    """
    for week in player["weeks"].values():
        for field in [k for k, v in week.items() if v in (None, 0, 0.0) and k != "week"]:
            del week[field]
    for section in ("season_totals", "season_averages", "recent_averages", "trend"):
        block = player.get(section)
        if isinstance(block, dict):
            for field in [k for k, v in block.items() if v is None]:
                del block[field]
    if player.get("role_note") is None:
        player.pop("role_note", None)
