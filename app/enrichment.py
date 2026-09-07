"""Optional external-source blocks attached to /snapshot.

Each block is opt-in through `?include=` so the default snapshot stays the
cheap Sleeper-only payload. A source that fails is reported inline as an
`error` on its own block rather than failing the whole snapshot — a missing
odds key should never cost you the roster.

Every source here now lives in the shared public-data service (see
app/public_client.py) rather than being computed locally, since none of it is
specific to this league. That service does its own upstream fetching, caching
and archiving; this module's job is orchestration — which players and NFL
teams matter for *this* league's roster — plus turning a proxy failure into
the same inline error shape the rest of /snapshot already expects.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.players import PlayerStore
from app.public_client import PublicDataClient
from app.teams import normalise_abbr

log = logging.getLogger(__name__)

VALID_INCLUDES = ("advanced_stats", "odds", "injury_report", "weather")


def parse_includes(include: str | None) -> list[str]:
    """Parse `?include=a,b`, rejecting unknown names with a helpful message."""
    if not include:
        return []
    requested = [part.strip().lower() for part in include.split(",") if part.strip()]
    unknown = [name for name in requested if name not in VALID_INCLUDES]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown include(s): {', '.join(unknown)}. "
                f"Valid values: {', '.join(VALID_INCLUDES)}."
            ),
        )
    # Preserve order and drop duplicates.
    return list(dict.fromkeys(requested))


def rostered_players(teams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every distinct player on any roster in the snapshot."""
    seen: dict[str, dict[str, Any]] = {}
    for team in teams:
        entries = [s.get("player") for s in team.get("starters", [])]
        entries += team.get("bench", []) + team.get("injured_reserve", [])
        entries += team.get("taxi_squad", [])
        for player in entries:
            if player and player.get("player_id") not in seen:
                seen[player["player_id"]] = player
    return list(seen.values())


def rostered_teams(players: list[dict[str, Any]]) -> list[str]:
    """The NFL teams that rostered players actually belong to."""
    abbrs = {normalise_abbr(p.get("nfl_team")) for p in players}
    return sorted(a for a in abbrs if a)


async def _guarded(name: str, coro: Any) -> dict[str, Any]:
    """Run one source, turning any failure into an inline error block."""
    try:
        return await coro
    except HTTPException as exc:
        log.warning("[%s] unavailable: %s", name, exc.detail)
        return {"available": False, "error": exc.detail}
    except Exception as exc:  # noqa: BLE001 - one bad source must not sink /snapshot
        log.exception("[%s] unexpected failure", name)
        return {"available": False, "error": str(exc)}


async def build_blocks(
    includes: list[str],
    *,
    public: PublicDataClient,
    players: PlayerStore,
    snapshot_teams: list[dict[str, Any]],
    week: int,
    season: int | None,
) -> dict[str, Any]:
    """Fetch every requested source concurrently and return their blocks."""
    if not includes:
        return {}

    roster_players = rostered_players(snapshot_teams)
    nfl_teams = rostered_teams(roster_players)

    tasks: dict[str, Any] = {}
    if "advanced_stats" in includes:
        tasks["advanced_stats"] = _guarded(
            "advanced_stats", advanced_stats_block(public, roster_players, season)
        )
    if "odds" in includes:
        tasks["odds"] = _guarded("odds", odds_block(public, week, season))
    if "injury_report" in includes:
        tasks["injury_report"] = _guarded(
            "injury_report", injury_block(public, players, roster_players, nfl_teams)
        )
    if "weather" in includes:
        tasks["weather"] = _guarded("weather", weather_block(public, week, season))

    results = await asyncio.gather(*tasks.values())
    return dict(zip(tasks.keys(), results))


# --- Individual blocks --------------------------------------------------------


async def advanced_stats_block(
    public: PublicDataClient,
    roster_players: list[dict[str, Any]],
    season: int | None,
) -> dict[str, Any]:
    """nflverse usage for every rostered player, keyed by Sleeper player id.

    One call per player to the public-data service, in parallel - a team
    defense (no gsis_id) or a player nflverse has no data for both come back
    as a 404, which is expected and sorted into `unmatched_players` rather
    than treated as a failure.
    """
    skill_players = [p for p in roster_players if p.get("position") != "DEF"]

    async def one(player: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        try:
            result = await public.get(
                f"/advanced-stats/{player['player_id']}", params={"season": season}
            )
        except HTTPException:
            return player, None
        return player, result

    results = await asyncio.gather(*[one(p) for p in skill_players])

    stats: dict[str, Any] = {}
    unmatched: list[str] = []
    season_out: int | None = season
    sources: list[str] | None = None
    for player, result in results:
        stats_block = (result or {}).get("stats") if result and result.get("found") else None
        if stats_block is None:
            unmatched.append(player["name"])
            continue
        if result.get("season") is not None:
            season_out = result["season"]
        sources = sources or result.get("sources")
        stats[player["player_id"]] = {
            "name": player["name"],
            "position": player.get("position"),
            "season_averages": stats_block.get("season_averages"),
            "recent_averages": stats_block.get("recent_averages"),
            "trend_vs_season": stats_block.get("trend_vs_season"),
            "role_note": stats_block.get("role_note"),
        }

    movers = sorted(
        (s for s in stats.values() if s.get("role_note")),
        key=lambda s: abs((s.get("trend_vs_season") or {}).get("snap_pct") or 0),
        reverse=True,
    )[:10]

    return {
        "available": True,
        "season": season_out or (season or datetime.now(timezone.utc).year),
        "sources": sources,
        "players": stats,
        "biggest_role_changes": movers,
        "unmatched_players": unmatched,
    }


async def odds_block(public: PublicDataClient, week: int, season: int | None) -> dict[str, Any]:
    payload = await public.get(f"/odds/{week}", params={"season": season})
    return {"available": True, **payload}


async def injury_block(
    public: PublicDataClient,
    players: PlayerStore,
    roster_players: list[dict[str, Any]],
    nfl_teams: list[str],
) -> dict[str, Any]:
    """ESPN injury reports for the teams that rostered players play for."""
    results = await asyncio.gather(
        *[public.get("/injury-report", params={"team": team}) for team in nfl_teams],
        return_exceptions=True,
    )

    by_espn_id: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    succeeded = 0
    for team, report in zip(nfl_teams, results):
        if isinstance(report, BaseException):
            failures.append(f"{team}: {getattr(report, 'detail', report)}")
            continue
        succeeded += 1
        for item in report.get("injuries", []):
            if item.get("espn_id"):
                by_espn_id[str(item["espn_id"])] = {**item, "nfl_team": team}
            if item.get("name"):
                by_name.setdefault(
                    " ".join(str(item["name"]).lower().split()), {**item, "nfl_team": team}
                )

    matched: dict[str, Any] = {}
    for player in roster_players:
        espn_id = players.espn_id(player["player_id"])
        item = by_espn_id.get(espn_id) if espn_id else None
        if item is None:
            item = by_name.get(" ".join(player["name"].lower().split()))
        if item is None:
            continue
        matched[player["player_id"]] = {
            "name": player["name"],
            "sleeper_status": player.get("injury_status"),
            "espn_status": item.get("status"),
            "practice_participation": item.get("practice_participation"),
            "injury_type": item.get("injury_type"),
            "updated": item.get("updated"),
            "comment": item.get("comment"),
        }

    if nfl_teams and not succeeded:
        # Every team failed; saying "available" here would hide the outage.
        return {
            "available": False,
            "error": f"The public-data service was unreachable for all {len(nfl_teams)} teams.",
            "teams_unavailable": failures,
        }

    return {
        "available": True,
        "teams_checked": nfl_teams,
        "teams_reported": succeeded,
        "players": matched,
        "teams_unavailable": failures,
    }


async def weather_block(public: PublicDataClient, week: int, season: int | None) -> dict[str, Any]:
    """Forecasts for the week's open-air venues; domes short-circuit.

    The public-data service already does this orchestration internally (it
    owns both the schedule and the weather provider), so this is a direct
    passthrough.
    """
    payload = await public.get(f"/weather/{week}", params={"season": season})
    return payload if "available" in payload else {"available": True, **payload}
