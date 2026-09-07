"""SQLite storage for the league's own history.

Everything here is data that either cannot be re-fetched later, or that is
expensive enough to re-fetch that it is worth keeping: transactions, draft
picks, weekly roster snapshots, betting lines, injury reports and the decision
log. Player stats and schedules are not stored - nflverse keeps those upstream
and they are re-fetched on demand.

Design notes:

  * The standard library's `sqlite3`, no ORM. At this size an ORM would be
    weight without benefit.
  * One connection, opened once, guarded by a lock; every call runs through
    `asyncio.to_thread` because sqlite3 blocks. WAL mode so a read never waits
    behind a write.
  * Every table keeps the upstream row verbatim in a `payload` column beside
    the extracted columns. Wanting a field later is then a query change rather
    than a migration plus a re-fetch of data that may no longer exist.
  * Migrations are versioned through `PRAGMA user_version` and applied in
    order at startup. Adding a migration means appending to MIGRATIONS; never
    edit one that has shipped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

log = logging.getLogger(__name__)

# Append only. Each entry is one schema version, applied in order.
MIGRATIONS: list[str] = [
    # 1 - league history
    """
    CREATE TABLE seasons (
        season              INTEGER PRIMARY KEY,
        league_id           TEXT NOT NULL,
        name                TEXT,
        status              TEXT,
        total_rosters       INTEGER,
        previous_league_id  TEXT,
        draft_id            TEXT,
        discovered_at       TEXT NOT NULL,
        payload             TEXT NOT NULL
    );
    CREATE UNIQUE INDEX ix_seasons_league ON seasons(league_id);

    CREATE TABLE managers (
        season        INTEGER NOT NULL,
        user_id       TEXT NOT NULL,
        roster_id     INTEGER,
        display_name  TEXT,
        team_name     TEXT,
        payload       TEXT NOT NULL,
        PRIMARY KEY (season, user_id)
    );

    CREATE TABLE transactions (
        transaction_id  TEXT NOT NULL,
        season          INTEGER NOT NULL,
        league_id       TEXT NOT NULL,
        week            INTEGER,
        type            TEXT,
        status          TEXT,
        created_ms      INTEGER,
        updated_ms      INTEGER,
        creator_user_id TEXT,
        roster_ids      TEXT,
        waiver_bid      INTEGER,
        payload         TEXT NOT NULL,
        PRIMARY KEY (season, transaction_id)
    );
    CREATE INDEX ix_tx_season_week ON transactions(season, week);
    CREATE INDEX ix_tx_type ON transactions(season, type);
    CREATE INDEX ix_tx_updated ON transactions(updated_ms);

    CREATE TABLE draft_picks (
        draft_id   TEXT NOT NULL,
        pick_no    INTEGER NOT NULL,
        season     INTEGER NOT NULL,
        round      INTEGER,
        roster_id  INTEGER,
        player_id  TEXT,
        position   TEXT,
        is_keeper  INTEGER,
        payload    TEXT NOT NULL,
        PRIMARY KEY (draft_id, pick_no)
    );
    CREATE INDEX ix_picks_season ON draft_picks(season);

    CREATE TABLE roster_snapshots (
        season       INTEGER NOT NULL,
        week         INTEGER NOT NULL,
        roster_id    INTEGER NOT NULL,
        captured_at  TEXT NOT NULL,
        wins         INTEGER,
        losses       INTEGER,
        ties         INTEGER,
        points_for   REAL,
        payload      TEXT NOT NULL,
        PRIMARY KEY (season, week, roster_id)
    );
    """,
    # 2 - the decision log. Betting lines and injury reports (odds_history /
    # injury_history) live in the shared public-data service instead, since
    # they are not specific to any one league.
    """
    CREATE TABLE decisions (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id          TEXT NOT NULL,
        logged_at            TEXT NOT NULL,
        season               INTEGER NOT NULL,
        week                 INTEGER,
        kind                 TEXT,
        summary              TEXT,
        reasoning            TEXT,
        players              TEXT,
        confidence           TEXT,
        expected             TEXT,
        outcome              TEXT,
        outcome_recorded_at  TEXT,
        payload              TEXT NOT NULL
    );
    CREATE INDEX ix_decisions_season ON decisions(season, week);
    CREATE INDEX ix_decisions_id ON decisions(decision_id, id);
    """,
]


class Database:
    """A single SQLite file holding everything the league needs remembered."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # --- Lifecycle ------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        # WAL lets a read proceed while a write is in flight; NORMAL is the
        # right durability trade for data that can mostly be re-fetched.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        self._connection = connection
        self._migrate(connection)
        return connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _migrate(self, connection: sqlite3.Connection) -> None:
        current = connection.execute("PRAGMA user_version").fetchone()[0]
        target = len(MIGRATIONS)
        if current > target:
            raise RuntimeError(
                f"The database at {self.path} is at schema version {current}, but this "
                f"build only knows up to {target}. It was written by a newer version; "
                "refusing to touch it rather than corrupting it."
            )
        if current == target:
            return

        for version in range(current, target):
            log.info("Applying database migration %s -> %s", version, version + 1)
            with self._lock:
                connection.execute("BEGIN")
                try:
                    # Statement by statement rather than executescript: in
                    # autocommit mode executescript commits any open
                    # transaction first, which would defeat the point of
                    # wrapping the migration in one.
                    for statement in _statements(MIGRATIONS[version]):
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version={version + 1}")
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
        log.info("Database schema is at version %s.", target)

    @property
    def schema_version(self) -> int:
        return self.connect().execute("PRAGMA user_version").fetchone()[0]

    # --- Sync primitives ------------------------------------------------------

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        connection = self.connect()
        with self._lock:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        connection = self.connect()
        with self._lock:
            cursor = connection.execute(sql, params)
            return cursor.rowcount

    def _execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        batch = list(rows)
        if not batch:
            return 0
        connection = self.connect()
        with self._lock:
            connection.execute("BEGIN")
            try:
                cursor = connection.executemany(sql, batch)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            return cursor.rowcount

    # --- Async wrappers -------------------------------------------------------

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._query, sql, params)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        return await asyncio.to_thread(self._execute, sql, params)

    async def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        return await asyncio.to_thread(self._execute_many, rows=rows, sql=sql)

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run an arbitrary sync function against the database off the loop."""
        return await asyncio.to_thread(fn, *args)

    # --- Introspection --------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        connection = self.connect()
        tables = [
            "seasons", "managers", "transactions", "draft_picks",
            "roster_snapshots", "decisions",
        ]
        counts: dict[str, int] = {}
        with self._lock:
            for table in tables:
                counts[table] = connection.execute(
                    f"SELECT count(*) FROM {table}"  # noqa: S608 - fixed list above
                ).fetchone()[0]
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "path": str(self.path),
            "schema_version": self.schema_version,
            "size_mb": round(size / 1_048_576, 3),
            "rows": counts,
        }


def _statements(script: str) -> list[str]:
    """Split a migration into individual statements.

    Safe for the plain CREATE TABLE / CREATE INDEX statements used here; a
    migration containing a trigger or any body with an inner semicolon would
    need a real parser instead.
    """
    return [part.strip() for part in script.split(";") if part.strip()]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
