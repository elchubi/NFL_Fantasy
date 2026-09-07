"""SQLite storage for the two sources that cannot be re-fetched later.

Betting lines and injury reports are public NFL data - unrelated to any
specific fantasy league - so they are archived once here rather than once per
league backend. Same design as the league backend's own database (see that
project's app/db.py for the fuller rationale): stdlib sqlite3, no ORM, WAL
mode, versioned migrations via PRAGMA user_version, and the upstream row kept
verbatim in a payload column beside the extracted ones.
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

MIGRATIONS: list[str] = [
    """
    CREATE TABLE odds_history (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at  TEXT NOT NULL,
        season       INTEGER NOT NULL,
        week         INTEGER NOT NULL,
        game_id      TEXT NOT NULL,
        home_team    TEXT,
        away_team    TEXT,
        home_spread  REAL,
        away_spread  REAL,
        total        REAL,
        favourite    TEXT,
        payload      TEXT NOT NULL
    );
    CREATE INDEX ix_odds_season_week ON odds_history(season, week);
    CREATE INDEX ix_odds_game ON odds_history(season, week, game_id, id);

    CREATE TABLE injury_history (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at             TEXT NOT NULL,
        season                  INTEGER NOT NULL,
        week                    INTEGER NOT NULL,
        team                    TEXT NOT NULL,
        espn_id                 TEXT,
        name                    TEXT NOT NULL,
        status                  TEXT,
        practice_participation  TEXT,
        injury_type             TEXT,
        payload                 TEXT NOT NULL
    );
    CREATE INDEX ix_injury_season_week ON injury_history(season, week);
    CREATE INDEX ix_injury_subject ON injury_history(season, week, team, name, id);
    """,
]


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
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
                f"build only knows up to {target}. Refusing to touch it."
            )
        for version in range(current, target):
            log.info("Applying database migration %s -> %s", version, version + 1)
            with self._lock:
                connection.execute("BEGIN")
                try:
                    for statement in _statements(MIGRATIONS[version]):
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version={version + 1}")
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise

    @property
    def schema_version(self) -> int:
        return self.connect().execute("PRAGMA user_version").fetchone()[0]

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        connection = self.connect()
        with self._lock:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

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

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._query, sql, params)

    async def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        return await asyncio.to_thread(self._execute_many, rows=rows, sql=sql)

    def stats(self) -> dict[str, Any]:
        connection = self.connect()
        counts: dict[str, int] = {}
        with self._lock:
            for table in ("odds_history", "injury_history"):
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
