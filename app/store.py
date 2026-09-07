"""Typed reads and writes over the SQLite database.

Writes are idempotent throughout. Transactions and draft picks are immutable
once complete, so re-archiving a season is a no-op rather than a duplicate.
Odds and injuries are append-only histories where a row is written only when
the subject actually changed, so the sequence of rows *is* the history.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.db import Database, dumps, loads

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _expand(row: dict[str, Any]) -> dict[str, Any]:
    """Merge the stored payload back over the extracted columns."""
    payload = loads(row.get("payload")) or {}
    merged = {k: v for k, v in row.items() if k != "payload"}
    merged.update(payload)
    return merged


# --- League history (immutable) ----------------------------------------------


async def upsert_season(db: Database, season: int, league: dict[str, Any]) -> None:
    await db.execute(
        """
        INSERT INTO seasons (season, league_id, name, status, total_rosters,
                             previous_league_id, draft_id, discovered_at, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(season) DO UPDATE SET
            league_id=excluded.league_id, name=excluded.name, status=excluded.status,
            total_rosters=excluded.total_rosters,
            previous_league_id=excluded.previous_league_id,
            draft_id=excluded.draft_id, payload=excluded.payload
        """,
        (
            season,
            league.get("league_id"),
            league.get("name"),
            league.get("status"),
            league.get("total_rosters"),
            league.get("previous_league_id"),
            league.get("draft_id"),
            _now(),
            dumps(league),
        ),
    )


async def seasons(db: Database) -> list[dict[str, Any]]:
    return await db.query("SELECT * FROM seasons ORDER BY season DESC")


async def known_season_numbers(db: Database) -> list[int]:
    rows = await db.query("SELECT season FROM seasons ORDER BY season DESC")
    return [r["season"] for r in rows]


async def upsert_managers(
    db: Database, season: int, users: list[dict[str, Any]], teams: dict[int, dict[str, Any]]
) -> int:
    by_user = {t.get("owner_id"): t for t in teams.values()}
    rows = [
        (
            season,
            user.get("user_id"),
            (by_user.get(user.get("user_id")) or {}).get("roster_id"),
            user.get("display_name"),
            (user.get("metadata") or {}).get("team_name"),
            dumps(user),
        )
        for user in users
        if user.get("user_id")
    ]
    return await db.execute_many(
        """
        INSERT INTO managers (season, user_id, roster_id, display_name, team_name, payload)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(season, user_id) DO UPDATE SET
            roster_id=excluded.roster_id, display_name=excluded.display_name,
            team_name=excluded.team_name, payload=excluded.payload
        """,
        rows,
    )


async def save_transactions(
    db: Database, season: int, league_id: str, transactions: list[dict[str, Any]]
) -> int:
    """Archive transactions. Completed ones never change, so this is a no-op on
    a re-run; a claim that moved from pending to complete is updated."""
    rows = []
    for tx in transactions:
        transaction_id = tx.get("transaction_id")
        if not transaction_id:
            continue
        settings = tx.get("settings") or {}
        rows.append(
            (
                transaction_id,
                season,
                league_id,
                _int(tx.get("leg")),
                tx.get("type"),
                tx.get("status"),
                _int(tx.get("created")),
                _int(tx.get("status_updated")),
                tx.get("creator"),
                dumps(tx.get("roster_ids") or []),
                settings.get("waiver_bid"),
                dumps(tx),
            )
        )
    return await db.execute_many(
        """
        INSERT INTO transactions (transaction_id, season, league_id, week, type, status,
                                  created_ms, updated_ms, creator_user_id, roster_ids,
                                  waiver_bid, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(season, transaction_id) DO UPDATE SET
            status=excluded.status, updated_ms=excluded.updated_ms,
            waiver_bid=excluded.waiver_bid, payload=excluded.payload
        """,
        rows,
    )


async def load_transactions(
    db: Database,
    *,
    seasons: list[int] | None = None,
    since_ms: float | None = None,
) -> list[dict[str, Any]]:
    """Raw Sleeper transaction payloads, so the existing analysis is unchanged."""
    clauses, params = [], []
    if seasons:
        clauses.append(f"season IN ({','.join('?' * len(seasons))})")
        params.extend(seasons)
    if since_ms is not None:
        clauses.append("COALESCE(updated_ms, created_ms) >= ?")
        params.append(since_ms)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await db.query(
        f"SELECT payload FROM transactions {where} ORDER BY COALESCE(updated_ms, created_ms)",  # noqa: S608
        params,
    )
    return [p for p in (loads(r["payload"]) for r in rows) if p]


async def save_draft_picks(
    db: Database, season: int, draft_id: str, picks: list[dict[str, Any]]
) -> int:
    rows = []
    for pick in picks:
        pick_no = pick.get("pick_no")
        if pick_no is None:
            continue
        metadata = pick.get("metadata") or {}
        rows.append(
            (
                draft_id,
                pick_no,
                season,
                pick.get("round"),
                pick.get("roster_id"),
                pick.get("player_id"),
                metadata.get("position"),
                1 if pick.get("is_keeper") else 0,
                dumps(pick),
            )
        )
    return await db.execute_many(
        """
        INSERT INTO draft_picks (draft_id, pick_no, season, round, roster_id,
                                 player_id, position, is_keeper, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(draft_id, pick_no) DO UPDATE SET
            round=excluded.round, roster_id=excluded.roster_id,
            player_id=excluded.player_id, position=excluded.position,
            is_keeper=excluded.is_keeper, payload=excluded.payload
        """,
        rows,
    )


async def load_draft_picks(
    db: Database, seasons: list[int] | None = None
) -> list[dict[str, Any]]:
    where, params = "", []
    if seasons:
        where = f"WHERE season IN ({','.join('?' * len(seasons))})"
        params = list(seasons)
    rows = await db.query(
        f"SELECT payload FROM draft_picks {where} ORDER BY season, pick_no", params  # noqa: S608
    )
    return [p for p in (loads(r["payload"]) for r in rows) if p]


async def save_roster_snapshot(
    db: Database, season: int, week: int, rosters: list[dict[str, Any]]
) -> int:
    captured = _now()
    rows = []
    for roster in rosters:
        roster_id = roster.get("roster_id")
        if roster_id is None:
            continue
        settings = roster.get("settings") or {}
        points = float(settings.get("fpts") or 0) + float(settings.get("fpts_decimal") or 0) / 100
        rows.append(
            (
                season, week, roster_id, captured,
                settings.get("wins"), settings.get("losses"), settings.get("ties"),
                round(points, 2), dumps(roster),
            )
        )
    return await db.execute_many(
        """
        INSERT INTO roster_snapshots (season, week, roster_id, captured_at,
                                      wins, losses, ties, points_for, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(season, week, roster_id) DO UPDATE SET
            captured_at=excluded.captured_at, wins=excluded.wins,
            losses=excluded.losses, ties=excluded.ties,
            points_for=excluded.points_for, payload=excluded.payload
        """,
        rows,
    )


async def load_roster_snapshot(
    db: Database, season: int, week: int
) -> list[dict[str, Any]]:
    rows = await db.query(
        "SELECT payload FROM roster_snapshots WHERE season=? AND week=? ORDER BY roster_id",
        (season, week),
    )
    return [p for p in (loads(r["payload"]) for r in rows) if p]


# --- Decision log -------------------------------------------------------------


async def append_decision(
    db: Database, season: int, week: int | None, decision: dict[str, Any]
) -> dict[str, Any]:
    await db.execute(
        """
        INSERT INTO decisions (decision_id, logged_at, season, week, kind, summary,
                               reasoning, players, confidence, expected, outcome,
                               outcome_recorded_at, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision["decision_id"], _now(), season, week,
            decision.get("kind"), decision.get("summary"), decision.get("reasoning"),
            dumps(decision.get("players") or []), decision.get("confidence"),
            decision.get("expected"), decision.get("outcome"),
            decision.get("outcome_recorded_at"), dumps(decision),
        ),
    )
    return {"source": "decisions", "written": 1}


async def find_decision(db: Database, season: int, decision_id: str) -> dict[str, Any] | None:
    rows = await db.query(
        "SELECT * FROM decisions WHERE season=? AND decision_id=? ORDER BY id",
        (season, decision_id),
    )
    return _expand(rows[-1]) if rows else None


async def read_decisions(
    db: Database, season: int, week: int | None = None
) -> list[dict[str, Any]]:
    """One entry per decision, with the latest recorded outcome applied.

    Outcomes are appended rather than edited, so the original call and its
    reasoning survive exactly as written; this collapses the rows for reading.
    """
    where, params = "WHERE season=?", [season]
    if week is not None:
        where += " AND week=?"
        params.append(week)
    rows = await db.query(f"SELECT * FROM decisions {where} ORDER BY id", params)  # noqa: S608

    collapsed: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = _expand(row)
        decision_id = entry.get("decision_id")
        if decision_id not in collapsed:
            collapsed[decision_id] = entry
        elif entry.get("outcome"):
            collapsed[decision_id]["outcome"] = entry["outcome"]
            collapsed[decision_id]["outcome_recorded_at"] = entry.get("outcome_recorded_at")
    return list(collapsed.values())


async def inventory(db: Database) -> dict[str, Any]:
    """What the archive holds, per source and season."""
    result: dict[str, Any] = {}
    for source, table in (
        ("decisions", "decisions"),
        ("transactions", "transactions"),
        ("draft_picks", "draft_picks"),
        ("roster_snapshots", "roster_snapshots"),
    ):
        has_week = table != "draft_picks"
        weeks = "GROUP_CONCAT(DISTINCT week)" if has_week else "NULL"
        rows = await db.query(
            f"SELECT season, count(*) AS rows, {weeks} AS weeks "  # noqa: S608
            f"FROM {table} GROUP BY season ORDER BY season DESC"
        )
        result[source] = {
            str(r["season"]): {
                "rows": r["rows"],
                "weeks": sorted({int(w) for w in (r["weeks"] or "").split(",") if w})
                if r["weeks"]
                else [],
            }
            for r in rows
        }
    return result
