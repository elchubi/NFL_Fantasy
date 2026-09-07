"""Capturing the two sources that cannot be re-fetched later.

Everything else this service reads stays available upstream: nflverse publishes
a file per season going back to 1999, Sleeper keeps past seasons reachable
through `previous_league_id`, and Open-Meteo has a historical archive API.

Two things do evaporate:

    odds      The free Odds API tier only returns upcoming games. Once a game
              kicks off its closing line is gone (historical odds are a paid
              add-on).
    injuries  ESPN has no historical endpoint at all. Wednesday's "limited" is
              overwritten by Thursday's "full", and once the week passes there
              is no record that either happened.

Both go into SQLite as append-only histories: a row is written only when the
subject actually changed, so the sequence of rows is the history. Storage lives
in `app/store.py`; this module is the capture logic over it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app import store
from app.db import Database

log = logging.getLogger(__name__)

SOURCES = ("odds", "injuries", "decisions")

DECISION_KINDS = (
    "waiver_bid", "trade", "start_sit", "draft_pick", "keeper", "drop", "other",
)

# ESPN is free and unmetered, but 32 simultaneous requests is impolite.
ESPN_CONCURRENCY = 6


# --- Row shaping --------------------------------------------------------------


def odds_rows(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim an odds payload to the fields worth keeping forever."""
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
    """Trim an ESPN team report to the fields worth keeping forever."""
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


def decision_row(
    *,
    kind: str,
    summary: str,
    reasoning: str | None = None,
    players: list[str] | None = None,
    confidence: str | None = None,
    expected: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    """One entry in the decision log.

    The point is not record-keeping for its own sake. Two seasons of these, read
    against what actually happened, is the only way to find out where your
    process is systematically wrong. No public tool can tell you, because none
    of them knows what you decided or why.
    """
    return {
        "decision_id": decision_id or uuid.uuid4().hex[:12],
        "kind": kind if kind in DECISION_KINDS else "other",
        "kind_raw": kind,
        "summary": summary,
        "reasoning": reasoning,
        "players": players or [],
        "confidence": confidence,
        "expected": expected,
        "outcome": None,
        "outcome_recorded_at": None,
    }


def outcome_row(original: dict[str, Any], outcome: str) -> dict[str, Any]:
    """The follow-up entry recording how a decision turned out.

    Appended rather than edited: the original call is preserved exactly as it
    was made, which is the part that matters when checking your own reasoning
    against what happened.
    """
    return {
        **{k: v for k, v in original.items() if not k.startswith("_")},
        "outcome": outcome,
        "outcome_recorded_at": datetime.now(timezone.utc).isoformat(),
    }


# --- Capture ------------------------------------------------------------------


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
    """Archive this week's betting lines and injury reports.

    `refresh=True` bypasses the read caches so the archived line is the one live
    at capture time rather than whatever a browsing request warmed the cache
    with hours earlier. That is the point of capturing on a schedule, so it
    defaults on.
    """
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
            # ttl=0 forces one live call, which is what makes this a real
            # closing-line snapshot instead of a copy of the cache.
            data, _ = await provider.cache.get_or_refresh(
                f"week:{season}:{week}", provider._fetch, ttl=0
            )
            games = data.get("games", [])
        else:
            games = (await provider.for_week(week, season)).get("games", [])
    except Exception as exc:  # noqa: BLE001 - a failed source must not abort the rest
        log.warning("Odds capture failed: %s", exc)
        return {"written": 0, "skipped": 0, "error": str(getattr(exc, "detail", exc))}

    result = await store.append_odds(db, season, week, odds_rows(games))
    result["games_seen"] = len(games)
    return result


async def _capture_injuries(
    db: Database,
    provider: Any,
    season: int,
    week: int,
    teams: list[str],
    refresh: bool,
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


# --- Opportunistic capture ----------------------------------------------------


async def auto_capture(
    db: Database,
    source: str,
    season: int | None,
    week: int | None,
    rows: list[dict[str, Any]],
) -> None:
    """Archive rows a read endpoint just pulled from upstream.

    Best-effort by design: this runs on the path of an ordinary read request, so
    a broken archive must never turn a working `/odds` call into a 500. Any
    failure is logged and swallowed.

    Only call this when the data actually came from upstream; archiving a cache
    hit would query the archive just to conclude nothing changed.
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
    except Exception as exc:  # noqa: BLE001 - never fail the read it rode in on
        log.warning("Auto-capture of %s failed: %s", source, exc)
