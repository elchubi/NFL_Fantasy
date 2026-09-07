"""Capturing the two sources that cannot be re-fetched later.

Same rationale as the league backend's own capture logic, moved here because
odds and injury reports are public NFL data, not tied to any specific league:
capturing them once here instead of once per league is the whole point of the
split.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app import store
from app.db import Database

log = logging.getLogger(__name__)

SOURCES = ("odds", "injuries")

# ESPN is free and unmetered, but 32 simultaneous requests is impolite.
ESPN_CONCURRENCY = 6


def odds_rows(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "game_id": game.get("game_id"),
            "kickoff": game.get("kickoff"),
            "home_team": game.get("home_team_abbr") or game.get("home_team"),
            "away_team": game.get("away_team_abbr") or game.get("away_team"),
            "home_spread": game.get("home_spread"),
            "away_spread": game.get("away_spread"),
            "total": game.get("total"),
            "favourite": game.get("favourite_abbr") or game.get("favourite"),
            "spread": game.get("spread"),
            "moneyline": game.get("moneyline"),
            "implied_team_totals": game.get("implied_team_totals"),
            "bookmakers_counted": game.get("bookmakers_counted"),
        }
        for game in games
        if game.get("game_id")
    ]


def injury_rows(team: str, injuries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "team": team,
            "espn_id": item.get("espn_id"),
            "name": item.get("name"),
            "position": item.get("position"),
            "status": item.get("status"),
            "practice_participation": item.get("practice_participation"),
            "injury_type": item.get("injury_type"),
            "return_date": item.get("return_date"),
            "espn_updated": item.get("updated"),
        }
        for item in injuries
        if item.get("name") or item.get("espn_id")
    ]


async def capture_week(
    db: Database,
    *,
    odds_provider: Any,
    espn_provider: Any,
    season: int,
    week: int,
    teams: list[str],
    refresh: bool = True,
) -> dict[str, Any]:
    odds_result, injuries_result = await asyncio.gather(
        _capture_odds(db, odds_provider, season, week, refresh),
        _capture_injuries(db, espn_provider, season, week, teams, refresh),
    )
    return {
        "season": season,
        "week": week,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "odds": odds_result,
        "injuries": injuries_result,
    }


async def _capture_odds(
    db: Database, provider: Any, season: int, week: int, refresh: bool
) -> dict[str, Any]:
    if not provider.configured:
        return {
            "written": 0,
            "skipped": 0,
            "error": "ODDS_API_KEY is not configured; nothing to archive.",
        }
    try:
        if refresh:
            data, _ = await provider.cache.get_or_refresh(
                f"week:{season}:{week}", provider._fetch, ttl=0
            )
            games = data.get("games", [])
        else:
            games = (await provider.for_week(week, season)).get("games", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("Odds capture failed: %s", exc)
        return {"written": 0, "skipped": 0, "error": str(getattr(exc, "detail", exc))}

    result = await store.append_odds(db, season, week, odds_rows(games))
    result["games_seen"] = len(games)
    return result


async def _capture_injuries(
    db: Database, provider: Any, season: int, week: int, teams: list[str], refresh: bool
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(ESPN_CONCURRENCY)

    async def one(team: str) -> tuple[str, Any]:
        async with semaphore:
            try:
                if refresh:
                    data, _ = await provider.cache.get_or_refresh(
                        f"team:{team}", lambda: provider._fetch_team(team), ttl=0
                    )
                    return team, data.get("injuries", [])
                report = await provider.team_report(team)
                return team, report.get("injuries", [])
            except Exception as exc:  # noqa: BLE001
                return team, exc

    results = await asyncio.gather(*[one(team) for team in teams])

    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for team, outcome in results:
        if isinstance(outcome, BaseException):
            failures.append(f"{team}: {getattr(outcome, 'detail', outcome)}")
            continue
        rows.extend(injury_rows(team, outcome))

    result = await store.append_injuries(db, season, week, rows)
    result["teams_captured"] = len(teams) - len(failures)
    if failures:
        result["teams_failed"] = failures
    return result


async def auto_capture(
    db: Database, source: str, season: int | None, week: int | None, rows: list[dict[str, Any]]
) -> None:
    """Archive rows a read endpoint just pulled from upstream.

    Best-effort by design: never let a broken archive turn a working read into
    a 500. Only call this when the data actually came from upstream.
    """
    if not rows or season is None or week is None:
        return
    try:
        if source == "odds":
            result = await store.append_odds(db, season, week, rows)
        elif source == "injuries":
            result = await store.append_injuries(db, season, week, rows)
        else:
            return
        if result.get("written"):
            log.info(
                "Auto-captured %s row(s) of %s for %s week %s.",
                result["written"], source, season, week,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Auto-capture of %s failed: %s", source, exc)
