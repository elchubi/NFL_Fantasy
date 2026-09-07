"""Optional external-source blocks attached to /snapshot.

Each block is opt-in through `?include=` so the default snapshot stays the
cheap Sleeper-only payload. A source that fails is reported inline as an
`error` on its own block rather than failing the whole snapshot — a missing
odds key should never cost you the roster.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.history import auto_capture, injury_rows, odds_rows  # noqa: F401
from app.players import PlayerStore
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
    providers: dict[str, Any],
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
            "advanced_stats",
            advanced_stats_block(providers["nflverse"], players, roster_players, season),
        )
    if "odds" in includes:
        tasks["odds"] = _guarded(
            "odds", odds_block(providers["odds"], week, season, providers.get("history"))
        )
    if "injury_report" in includes:
        tasks["injury_report"] = _guarded(
            "injury_report",
            injury_block(
                providers["espn"],
                players,
                roster_players,
                nfl_teams,
                season,
                week,
                providers.get("history"),
            ),
        )
    if "weather" in includes:
        tasks["weather"] = _guarded(
            "weather", weather_block(providers["espn"], providers["weather"], week, season)
        )

    results = await asyncio.gather(*tasks.values())
    return dict(zip(tasks.keys(), results))


# --- Individual blocks --------------------------------------------------------


async def advanced_stats_block(
    nflverse: Any,
    players: PlayerStore,
    roster_players: list[dict[str, Any]],
    season: int | None,
) -> dict[str, Any]:
    """nflverse usage for every rostered player, keyed by Sleeper player id."""
    data, meta = await nflverse.season_data(season or datetime.now(timezone.utc).year)
    by_gsis = data.get("players") or {}

    stats: dict[str, Any] = {}
    unmatched: list[str] = []
    for player in roster_players:
        gsis = players.gsis_id(player["player_id"])
        entry = by_gsis.get(gsis) if gsis else None
        if entry is None:
            # Team defenses have no gsis_id at all; that is expected.
            if player.get("position") != "DEF":
                unmatched.append(player["name"])
            continue
        stats[player["player_id"]] = {
            "name": player["name"],
            "position": player.get("position"),
            "season_averages": entry.get("season_averages"),
            "recent_averages": entry.get("recent_averages"),
            "trend_vs_season": entry.get("trend"),
            "role_note": entry.get("role_note"),
        }

    movers = sorted(
        (s for s in stats.values() if s.get("role_note")),
        key=lambda s: abs((s.get("trend_vs_season") or {}).get("snap_pct") or 0),
        reverse=True,
    )[:10]

    return {
        "available": True,
        "season": data.get("season"),
        "sources": data.get("sources"),
        "players": stats,
        "biggest_role_changes": movers,
        "unmatched_players": unmatched,
        "cache": meta,
    }


async def odds_block(
    odds: Any, week: int, season: int | None, history: Any = None
) -> dict[str, Any]:
    if not odds.configured:
        return {
            "available": False,
            "error": "ODDS_API_KEY is not configured; set it to enable betting lines.",
        }
    payload = await odds.for_week(week, season)
    if history is not None and (payload.get("cache") or {}).get("refreshed"):
        await auto_capture(history, "odds", season, week, odds_rows(payload["games"]))
    return {"available": True, **payload}


async def injury_block(
    espn: Any,
    players: PlayerStore,
    roster_players: list[dict[str, Any]],
    nfl_teams: list[str],
    season: int | None = None,
    week: int | None = None,
    history: Any = None,
) -> dict[str, Any]:
    """ESPN injury reports for the teams that rostered players play for."""
    reports = await asyncio.gather(
        *[espn.team_report(team) for team in nfl_teams], return_exceptions=True
    )

    by_espn_id: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    fresh: list[dict[str, Any]] = []
    succeeded = 0
    for team, report in zip(nfl_teams, reports):
        if isinstance(report, BaseException):
            failures.append(f"{team}: {getattr(report, 'detail', report)}")
            continue
        succeeded += 1
        if history is not None and (report.get("cache") or {}).get("refreshed"):
            fresh.extend(injury_rows(team, report.get("injuries") or []))
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

    if fresh:
        await auto_capture(history, "injuries", season, week, fresh)

    if nfl_teams and not succeeded:
        # Every team failed; saying "available" here would hide the outage.
        return {
            "available": False,
            "error": f"ESPN was unreachable for all {len(nfl_teams)} teams.",
            "teams_unavailable": failures,
        }

    return {
        "available": True,
        "teams_checked": nfl_teams,
        "teams_reported": succeeded,
        "players": matched,
        "teams_unavailable": failures,
    }


async def weather_block(
    espn: Any, weather: Any, week: int, season: int | None
) -> dict[str, Any]:
    """Forecasts for the week's open-air venues; domes short-circuit."""
    schedule = await espn.schedule(week, season)
    games = schedule.get("games") or []
    if not games:
        return {
            "available": False,
            "error": (
                "No schedule available from ESPN for this week, so there are no "
                "venues to fetch weather for."
            ),
        }

    forecasts = await asyncio.gather(
        *[
            weather.for_venue(game["home_team"], _kickoff(game.get("kickoff")))
            for game in games
            if game.get("home_team")
        ],
        return_exceptions=True,
    )

    results = []
    for game, forecast in zip([g for g in games if g.get("home_team")], forecasts):
        if isinstance(forecast, BaseException):
            results.append(
                {
                    "game": game.get("name"),
                    "home_team": game.get("home_team"),
                    "error": str(getattr(forecast, "detail", forecast)),
                }
            )
            continue
        results.append({"game": game.get("name"), "away_team": game.get("away_team"), **forecast})

    return {
        "available": True,
        "week": week,
        "games": results,
        "outdoor_games_with_concerns": [
            g
            for g in results
            if (g.get("weather") or {}).get("fantasy_impact", {}).get("severity")
            in ("moderate", "high")
        ],
    }


def _kickoff(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
