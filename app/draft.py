"""Rookie draft board built entirely from nflverse releases.

In a keeper league with one keeper you never stash prospects, so a rookie only
matters if he produces in his rookie season. What predicts that, roughly in
order of strength:

    1. draft capital        where the NFL took him
    2. age at draft         younger is better, especially at WR
    3. landing spot         how much work actually vacated ahead of him
    4. college market share the one thing that needs an NCAA source
    5. athletic testing     combine measurables

Four of those five come from two small nflverse files - draft_picks (which
carries `gsis_id`, so it joins straight onto Sleeper rosters) and combine - plus
the snap counts this service already pulls. Only #4 is missing, and it is the
weakest of the five, so no college data source is used here at all.

    draft_picks/draft_picks.csv   ~1.6MB   season, round, pick, team, age, college
    combine/combine.csv           ~0.9MB   height, weight, forty, vertical, ...
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

from app.cache import KeyedDiskCache
from app.config import get_settings
from app.nflverse import _download_and_parse
from app.players import PlayerStore
from app.teams import normalise_abbr

log = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = 1

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")

COMBINE_FIELDS = {
    "ht": "height",
    "wt": "weight",
    "forty": "forty",
    "bench": "bench",
    "vertical": "vertical",
    "broad_jump": "broad_jump",
    "cone": "cone",
    "shuttle": "shuttle",
}


def _num(value: Any) -> float | None:
    if value in (None, "", "NA"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


class DraftProvider:
    def __init__(self, client: httpx.AsyncClient) -> None:
        settings = get_settings()
        self._client = client
        self._settings = settings
        self._base_url = settings.nflverse_base_url.rstrip("/")
        self.cache = KeyedDiskCache(
            settings.cache_path("draft_cache.json"),
            name="draft",
            default_ttl_seconds=settings.nflverse_cache_ttl_hours * 3600,
            schema_version=CACHE_SCHEMA_VERSION,
        )

    async def class_for(self, season: int) -> tuple[dict[str, Any], dict[str, Any]]:
        return await self.cache.get_or_refresh(
            f"class:{season}", lambda: self._build_class(season)
        )

    async def _build_class(self, season: int) -> dict[str, Any]:
        picks = await _download_and_parse(
            self._client,
            self._base_url,
            "draft_picks",
            "draft_picks.csv",
            lambda path: _parse_draft_picks(path, season),
            self._settings.nflverse_download_timeout,
        )
        combine = await _download_and_parse(
            self._client,
            self._base_url,
            "combine",
            "combine.csv",
            lambda path: _parse_combine(path, season),
            self._settings.nflverse_download_timeout,
        )
        picks = picks or {}
        combine = combine or {"by_pfr": {}, "by_cfb": {}}

        matched = 0
        for prospect in picks.values():
            measurables = combine["by_pfr"].get(prospect.get("pfr_id")) or combine[
                "by_cfb"
            ].get(prospect.get("cfb_id"))
            if measurables:
                matched += 1
            # Late-round picks are often not invited to the combine; that is a
            # missing measurement, not a missing player.
            prospect["combine"] = measurables

        return {
            "season": season,
            "prospects": picks,
            "counts": {
                "skill_picks": len(picks),
                "with_combine": matched,
                "with_gsis_id": sum(1 for p in picks.values() if p.get("gsis_id")),
            },
        }


# --- Parsers (sync; run in a worker thread) -----------------------------------


def _parse_draft_picks(path: Path, season: int) -> dict[str, Any]:
    """Skill-position picks for one draft class, keyed by gsis_id."""
    prospects: dict[str, Any] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("season") != str(season):
                continue
            if row.get("position") not in SKILL_POSITIONS:
                continue
            gsis = row.get("gsis_id") or ""
            # Without a gsis_id there is no way to line the pick up with a
            # Sleeper roster, so key on the pfr id and flag it.
            key = gsis or f"pfr:{row.get('pfr_player_id')}"
            prospects[key] = {
                "gsis_id": gsis or None,
                "pfr_id": row.get("pfr_player_id") or None,
                "cfb_id": row.get("cfb_player_id") or None,
                "name": row.get("pfr_player_name"),
                "position": row.get("position"),
                "college": row.get("college") or None,
                "draft": {
                    "season": season,
                    "round": _int(row.get("round")),
                    "pick_in_round": _int(row.get("pick")),
                    "team": normalise_abbr(row.get("team")),
                    "team_raw": row.get("team"),
                    "age": _num(row.get("age")),
                },
            }
    # draft_picks numbers picks within the round, so overall order is the file
    # order within a season; recompute it explicitly.
    for overall, prospect in enumerate(
        sorted(
            prospects.values(),
            key=lambda p: (p["draft"]["round"] or 99, p["draft"]["pick_in_round"] or 999),
        ),
        start=1,
    ):
        prospect["draft"]["skill_position_order"] = overall
    return prospects


def _parse_combine(path: Path, season: int) -> dict[str, Any]:
    by_pfr: dict[str, Any] = {}
    by_cfb: dict[str, Any] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("season") != str(season):
                continue
            measurables = {
                name: _num(row.get(source)) for source, name in COMBINE_FIELDS.items()
            }
            measurables = {k: v for k, v in measurables.items() if v is not None}
            if not measurables:
                continue
            measurables["school"] = row.get("school") or None
            if row.get("pfr_id"):
                by_pfr[row["pfr_id"]] = measurables
            if row.get("cfb_id"):
                by_cfb[row["cfb_id"]] = measurables
    return {"by_pfr": by_pfr, "by_cfb": by_cfb}


# --- Landing spot -------------------------------------------------------------


def landing_spot(
    prospect: dict[str, Any],
    prior_season: dict[str, Any],
    players: PlayerStore,
) -> dict[str, Any]:
    """How much work actually vacated at this player's position on his new team.

    Uses last season's snap share for everyone who played that position for the
    drafting team, then checks each one's *current* team in the Sleeper player
    file. Someone whose Sleeper team is no longer the drafting team has left,
    and their snaps are vacated. Both halves come from data this service already
    holds - no new source, and it is the most actionable thing on draft day: a
    mid-round RB walking into an empty backfield is worth more than a higher
    pick stuck behind a healthy starter.
    """
    team = prospect["draft"]["team"]
    position = prospect["position"]
    if not team:
        return {"available": False, "reason": "Unknown drafting team."}

    incumbents = []
    for entry in (prior_season.get("players") or {}).values():
        if entry.get("position") != position:
            continue
        if normalise_abbr(entry.get("team")) != team:
            continue
        snap_pct = (entry.get("season_averages") or {}).get("snap_pct")
        if not snap_pct:
            continue

        sleeper_id = players.sleeper_id_for_gsis(entry.get("gsis_id") or "")
        current_team = None
        if sleeper_id:
            current_team = normalise_abbr((players.raw(sleeper_id) or {}).get("team"))
        # No Sleeper record at all means out of the league entirely.
        departed = current_team != team

        incumbents.append(
            {
                "name": entry.get("name"),
                "gsis_id": entry.get("gsis_id"),
                "prior_snap_pct": snap_pct,
                "prior_targets_per_game": (entry.get("season_averages") or {}).get("targets"),
                "prior_red_zone_touches_per_game": (entry.get("season_averages") or {}).get(
                    "red_zone_touches"
                ),
                "current_team": current_team,
                "still_on_team": not departed,
            }
        )

    incumbents.sort(key=lambda i: i["prior_snap_pct"], reverse=True)
    vacated_points = sum(i["prior_snap_pct"] for i in incumbents if not i["still_on_team"])
    returning_points = sum(i["prior_snap_pct"] for i in incumbents if i["still_on_team"])
    total_points = vacated_points + returning_points

    # Snap percentages are per player, and three receivers are on the field at
    # once, so these sums are not bounded by 1 and adding them up as if they
    # were a share of one pie is meaningless. What is meaningful is the
    # proportion of the position's snap workload that walked out the door.
    vacated_share = round(vacated_points / total_points, 3) if total_points else 0.0
    top_returning = next((i for i in incumbents if i["still_on_team"]), None)

    return {
        "available": True,
        "team": team,
        "position": position,
        "prior_season": prior_season.get("season"),
        "vacated_share": vacated_share,
        "vacated_snap_points": round(vacated_points, 3),
        "returning_snap_points": round(returning_points, 3),
        "incumbents": incumbents,
        "opportunity": _opportunity_note(position, vacated_share, top_returning),
    }


def _opportunity_note(
    position: str, vacated_share: float, top_returning: dict[str, Any] | None
) -> str:
    blocker = (
        f"{top_returning['name']} returns having played {top_returning['prior_snap_pct'] * 100:.0f}% "
        "of snaps"
        if top_returning
        else "nobody established returns at the position"
    )
    if vacated_share >= 0.6:
        level = "wide open"
    elif vacated_share >= 0.3:
        level = "meaningful opening"
    elif vacated_share > 0:
        level = "modest opening"
    else:
        level = "no vacated work"
    return (
        f"{level} at {position}: {vacated_share * 100:.0f}% of last season's "
        f"{position} snap workload left the team; {blocker}."
    )


def require_prospect(class_data: dict[str, Any], key: str) -> dict[str, Any]:
    prospects = class_data.get("prospects") or {}
    prospect = prospects.get(key)
    if prospect is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No skill-position pick in the {class_data.get('season')} class matches "
                f"'{key}'. Only QB/RB/WR/TE picks are indexed."
            ),
        )
    return prospect
