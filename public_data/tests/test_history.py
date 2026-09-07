"""Offline tests for the odds/injury archive and the capture flow."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

from app import store  # noqa: E402
from app.db import Database  # noqa: E402
from app.history import capture_week, injury_rows, odds_rows  # noqa: E402
from app.odds import parse_game  # noqa: E402
from tests import fixtures_external as fx  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "public.db")
    database.connect()
    yield database
    database.close()


def _odds(spread=-9.0, total=50.0):
    return [
        {
            "game_id": "g1", "home_team_abbr": "KC", "away_team_abbr": "DET",
            "home_spread": spread, "away_spread": -spread, "total": total,
            "favourite_abbr": "KC", "moneyline": {"home": -350, "away": 280},
        }
    ]


def _injuries(status="Questionable", practice="limited"):
    return [
        {
            "espn_id": "3117251", "name": "Christian McCaffrey", "status": status,
            "practice_participation": practice, "injury_type": "Achilles",
        }
    ]


def test_migrations_run_once_and_are_idempotent(tmp_path):
    path = tmp_path / "public.db"
    first = Database(path)
    first.connect()
    version = first.schema_version
    assert version > 0
    first.close()

    second = Database(path)
    second.connect()
    assert second.schema_version == version
    second.close()


async def test_an_unchanged_line_is_not_written_twice(db):
    first = await store.append_odds(db, 2025, 2, odds_rows(_odds()))
    second = await store.append_odds(db, 2025, 2, odds_rows(_odds()))
    assert first["written"] == 1
    assert second["written"] == 0 and second["skipped"] == 1
    assert len(await store.read_odds(db, 2025, week=2)) == 1


async def test_a_line_move_is_recorded_as_a_new_row(db):
    await store.append_odds(db, 2025, 2, odds_rows(_odds(spread=-9.0)))
    await store.append_odds(db, 2025, 2, odds_rows(_odds(spread=-6.5)))
    rows = await store.read_odds(db, 2025, week=2)
    assert [r["home_spread"] for r in rows] == [-9.0, -6.5]


async def test_practice_participation_changes_are_each_recorded(db):
    for practice in ("did_not_practice", "limited", "limited", "full"):
        await store.append_injuries(db, 2025, 2, injury_rows("SF", _injuries(practice=practice)))
    rows = await store.read_injuries(db, 2025, week=2)
    assert [r["practice_participation"] for r in rows] == ["did_not_practice", "limited", "full"]


async def test_only_the_durable_fields_are_kept(db):
    game = parse_game(fx.ODDS_PAYLOAD[0])
    await store.append_odds(db, 2025, 2, odds_rows([game]))
    row = (await store.read_odds(db, 2025, week=2))[0]
    assert row["home_team"] == "KC" and row["favourite"] == "KC"
    assert "bookmakers" not in row


async def test_inventory_reports_what_is_archived(db):
    await store.append_odds(db, 2025, 2, odds_rows(_odds()))
    await store.append_injuries(db, 2025, 2, injury_rows("SF", _injuries()))
    inventory = await store.inventory(db)
    assert inventory["odds"]["2025"]["rows"] == 1
    assert inventory["injuries"]["2025"]["rows"] == 1


class _FakeCache:
    def __init__(self, data):
        self._data = data

    async def get_or_refresh(self, key, loader, ttl=None):
        return self._data, {}


class _FakeOdds:
    configured = True

    def __init__(self, games):
        self.cache = _FakeCache({"games": games})

    async def _fetch(self):  # pragma: no cover
        return {"games": []}


class _FakeEspn:
    def __init__(self, per_team):
        self._per_team = per_team
        self.cache = self

    async def get_or_refresh(self, key, loader, ttl=None):
        outcome = self._per_team[key.split(":", 1)[1]]
        if isinstance(outcome, Exception):
            raise outcome
        return {"injuries": outcome}, {}

    async def _fetch_team(self, team):  # pragma: no cover
        return {"injuries": []}


async def test_capture_week_writes_both_sources(db):
    result = await capture_week(
        db,
        odds_provider=_FakeOdds([parse_game(fx.ODDS_PAYLOAD[0])]),
        espn_provider=_FakeEspn({"SF": _injuries(), "KC": []}),
        season=2025, week=2, teams=["SF", "KC"],
    )
    assert result["odds"]["written"] == 1
    assert result["injuries"]["written"] == 1
    assert result["injuries"]["teams_captured"] == 2


async def test_capture_survives_one_team_failing(db):
    result = await capture_week(
        db,
        odds_provider=_FakeOdds([parse_game(fx.ODDS_PAYLOAD[0])]),
        espn_provider=_FakeEspn({"SF": _injuries(), "KC": RuntimeError("ESPN 500")}),
        season=2025, week=2, teams=["SF", "KC"],
    )
    assert result["injuries"]["written"] == 1
    assert result["injuries"]["teams_captured"] == 1
    assert "KC: ESPN 500" in result["injuries"]["teams_failed"]


async def test_capture_without_an_odds_key_still_archives_injuries(db):
    class Unconfigured:
        configured = False

    result = await capture_week(
        db, odds_provider=Unconfigured(), espn_provider=_FakeEspn({"SF": _injuries()}),
        season=2025, week=2, teams=["SF"],
    )
    assert result["odds"]["written"] == 0
    assert "ODDS_API_KEY" in result["odds"]["error"]
    assert result["injuries"]["written"] == 1


async def test_auto_capture_never_raises(db):
    from app.history import auto_capture

    await auto_capture(db, "odds", 2025, 2, odds_rows(_odds()))
    assert len(await store.read_odds(db, 2025, week=2)) == 1

    class Broken:
        async def query(self, *a, **k):
            raise RuntimeError("disk on fire")

        async def execute_many(self, *a, **k):
            raise RuntimeError("disk on fire")

    await auto_capture(Broken(), "odds", 2025, 2, odds_rows(_odds()))
    await auto_capture(db, "odds", None, 2, odds_rows(_odds()))
    await auto_capture(db, "odds", 2025, 2, [])
