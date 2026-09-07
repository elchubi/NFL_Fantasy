"""Typed reads and writes over the odds/injury archive.

Append-only: a row is written only when the subject actually changed, so the
sequence of rows is the history rather than a pile of identical snapshots.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.db import Database, dumps, loads

log = logging.getLogger(__name__)

ODDS_CHANGE_FIELDS = ("home_spread", "away_spread", "total", "favourite")
INJURY_CHANGE_FIELDS = ("status", "practice_participation", "injury_type", "return_date")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _same(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def _expand(row: dict[str, Any]) -> dict[str, Any]:
    payload = loads(row.get("payload")) or {}
    merged = {k: v for k, v in row.items() if k != "payload"}
    merged.update(payload)
    return merged


async def append_odds(
    db: Database, season: int, week: int, games: list[dict[str, Any]]
) -> dict[str, Any]:
    latest = {
        row["game_id"]: row
        for row in await db.query(
            """
            SELECT o.* FROM odds_history o
            JOIN (SELECT game_id, MAX(id) AS id FROM odds_history
                  WHERE season=? AND week=? GROUP BY game_id) newest
              ON o.id = newest.id
            """,
            (season, week),
        )
    }

    captured, rows, skipped = _now(), [], 0
    for game in games:
        game_id = game.get("game_id")
        if not game_id:
            continue
        previous = latest.get(game_id)
        if previous and all(
            _same(game.get(field), previous.get(field)) for field in ODDS_CHANGE_FIELDS
        ):
            skipped += 1
            continue
        rows.append(
            (
                captured, season, week, game_id,
                game.get("home_team"), game.get("away_team"),
                game.get("home_spread"), game.get("away_spread"),
                game.get("total"), game.get("favourite"), dumps(game),
            )
        )

    written = await db.execute_many(
        """
        INSERT INTO odds_history (captured_at, season, week, game_id, home_team,
                                  away_team, home_spread, away_spread, total,
                                  favourite, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return {"source": "odds", "written": len(rows) if written else 0, "skipped": skipped}


async def append_injuries(
    db: Database, season: int, week: int, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    latest = {
        (row["team"], row["name"]): row
        for row in await db.query(
            """
            SELECT i.* FROM injury_history i
            JOIN (SELECT team, name, MAX(id) AS id FROM injury_history
                  WHERE season=? AND week=? GROUP BY team, name) newest
              ON i.id = newest.id
            """,
            (season, week),
        )
    }

    captured, rows, skipped = _now(), [], 0
    for entry in entries:
        team, name = entry.get("team"), entry.get("name")
        if not team or not name:
            continue
        previous = latest.get((team, name))
        if previous:
            previous_payload = loads(previous.get("payload")) or {}
            if all(
                _same(entry.get(field), previous_payload.get(field))
                for field in INJURY_CHANGE_FIELDS
            ):
                skipped += 1
                continue
        rows.append(
            (
                captured, season, week, team, entry.get("espn_id"), name,
                entry.get("status"), entry.get("practice_participation"),
                entry.get("injury_type"), dumps(entry),
            )
        )

    written = await db.execute_many(
        """
        INSERT INTO injury_history (captured_at, season, week, team, espn_id, name,
                                    status, practice_participation, injury_type, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return {"source": "injuries", "written": len(rows) if written else 0, "skipped": skipped}


async def read_odds(
    db: Database, season: int, week: int | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    where, params = "WHERE season=?", [season]
    if week is not None:
        where += " AND week=?"
        params.append(week)
    rows = await db.query(f"SELECT * FROM odds_history {where} ORDER BY id", params)  # noqa: S608
    rows = rows[-limit:] if limit else rows
    return [_expand(row) for row in rows]


async def read_injuries(
    db: Database, season: int, week: int | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    where, params = "WHERE season=?", [season]
    if week is not None:
        where += " AND week=?"
        params.append(week)
    rows = await db.query(f"SELECT * FROM injury_history {where} ORDER BY id", params)  # noqa: S608
    rows = rows[-limit:] if limit else rows
    return [_expand(row) for row in rows]


async def inventory(db: Database) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source, table in (("odds", "odds_history"), ("injuries", "injury_history")):
        rows = await db.query(
            f"SELECT season, count(*) AS rows, GROUP_CONCAT(DISTINCT week) AS weeks "  # noqa: S608
            f"FROM {table} GROUP BY season ORDER BY season DESC"
        )
        result[source] = {
            str(r["season"]): {
                "rows": r["rows"],
                "weeks": sorted({int(w) for w in (r["weeks"] or "").split(",") if w}),
            }
            for r in rows
        }
    return result
