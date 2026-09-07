"""Walk the league's season chain and archive everything immutable.

In Sleeper each season is a **separate league** with its own `league_id`, linked
backwards by `previous_league_id`. A single configured LEAGUE_ID therefore only
ever reaches the current season - which is why manager profiling was
single-season until this existed, and why a `days` parameter larger than one
season could never return more data.

Transactions, draft picks and final standings do not change once a season is
over, so they are fetched once, stored, and never requested again. Re-running a
backfill is a no-op for seasons already archived unless `refresh` is set.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app import services, store
from app.db import Database
from app.sleeper import SleeperClient

log = logging.getLogger(__name__)

# Sleeper weeks run 1-18 for the regular season, plus playoff legs. Transactions
# are filed under the leg they happened in, so this covers a whole season.
ALL_WEEKS = range(1, 19)

# A chain longer than this means something is looping; leagues do not run that
# long and a cycle would otherwise fetch forever.
MAX_SEASONS = 20


async def discover_chain(
    client: SleeperClient, league_id: str, limit: int = MAX_SEASONS
) -> list[dict[str, Any]]:
    """Every league in the chain, newest first, following previous_league_id."""
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: str | None = league_id

    while current and current not in seen and len(chain) < limit:
        seen.add(current)
        try:
            league = await client.league(current)
        except Exception as exc:  # noqa: BLE001 - a broken link ends the chain
            log.warning("Stopping the season chain at %s: %s", current, exc)
            break
        chain.append(league)
        previous = league.get("previous_league_id")
        # Sleeper uses "0" and "" for "this is the first season".
        current = previous if previous and previous not in ("0", "null") else None

    return chain


async def backfill_season(
    client: SleeperClient,
    db: Database,
    league: dict[str, Any],
    *,
    weeks: range = ALL_WEEKS,
) -> dict[str, Any]:
    """Archive one season's transactions, draft picks, rosters and managers."""
    league_id = league.get("league_id")
    try:
        season = int(league.get("season"))
    except (TypeError, ValueError):
        return {"league_id": league_id, "error": "League has no usable season."}

    await store.upsert_season(db, season, league)

    users, rosters, drafts = await asyncio.gather(
        client.users(league_id), client.rosters(league_id), client.drafts(league_id)
    )
    teams = services.build_teams(users, rosters)
    await store.upsert_managers(db, season, users, teams)

    pages = await asyncio.gather(
        *[client.transactions(league_id, week) for week in weeks]
    )
    transactions = [tx for page in pages for tx in page]
    written = await store.save_transactions(db, season, league_id, transactions)

    picks_written = 0
    for draft in drafts:
        draft_id = draft.get("draft_id")
        if not draft_id:
            continue
        picks = await client.draft_picks(draft_id)
        picks_written += await store.save_draft_picks(db, season, draft_id, picks)

    # The end-of-season roster is worth keeping as the final standing; during a
    # live season this is simply the current state, refreshed each backfill.
    final_week = max(weeks)
    await store.save_roster_snapshot(db, season, final_week, rosters)

    return {
        "season": season,
        "league_id": league_id,
        "name": league.get("name"),
        "status": league.get("status"),
        "transactions_seen": len(transactions),
        "transactions_written": written,
        "draft_picks_written": picks_written,
        "managers": len(users),
    }


async def backfill_all(
    client: SleeperClient,
    db: Database,
    league_id: str,
    *,
    refresh: bool = False,
    limit: int = MAX_SEASONS,
) -> dict[str, Any]:
    """Archive every season in the chain.

    Seasons already stored are skipped unless `refresh` is set, since a finished
    season cannot change - the exception is the season in progress, which is
    always re-read.
    """
    chain = await discover_chain(client, league_id, limit=limit)
    if not chain:
        return {"seasons": [], "error": f"Could not read league {league_id}."}

    already = set(await store.known_season_numbers(db))
    results, skipped = [], []

    for league in chain:
        try:
            season = int(league.get("season"))
        except (TypeError, ValueError):
            continue
        in_progress = league.get("status") not in ("complete", "post_season")
        if season in already and not refresh and not in_progress:
            skipped.append(season)
            continue
        results.append(await backfill_season(client, db, league))

    return {
        "chain_length": len(chain),
        "seasons_archived": results,
        "seasons_skipped": sorted(skipped, reverse=True),
        "note": (
            "Finished seasons are archived once and skipped afterwards; the season "
            "in progress is always re-read. Pass refresh=true to force all of them."
        ),
    }
