"""Offline tests for the SQLite archive and the capture flow."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LEAGUE_ID", "1390746710426255360")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

from app import store  # noqa: E402
from app.db import Database  # noqa: E402
from app.history import capture_week, decision_row, injury_rows, odds_rows, outcome_row  # noqa: E402
from app.odds import parse_game  # noqa: E402
from tests import fixtures_external as fx  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "league.db")
    database.connect()
    yield database
    database.close()


def _odds(spread=-9.0, total=50.0):
    return [
        {
            "game_id": "g1",
            "home_team_abbr": "KC",
            "away_team_abbr": "DET",
            "home_spread": spread,
            "away_spread": -spread,
            "total": total,
            "favourite_abbr": "KC",
            "moneyline": {"home": -350, "away": 280},
        }
    ]


def _injuries(status="Questionable", practice="limited"):
    return [
        {
            "espn_id": "3117251",
            "name": "Christian McCaffrey",
            "status": status,
            "practice_participation": practice,
            "injury_type": "Achilles",
        }
    ]


# --- Schema -------------------------------------------------------------------


def test_migrations_run_once_and_are_idempotent(tmp_path):
    path = tmp_path / "league.db"
    first = Database(path)
    first.connect()
    version = first.schema_version
    assert version > 0
    first.close()

    # Re-opening an existing database must not re-apply anything.
    second = Database(path)
    second.connect()
    assert second.schema_version == version
    assert second.stats()["rows"]["transactions"] == 0
    second.close()


def test_a_newer_schema_is_refused_rather_than_downgraded(tmp_path):
    path = tmp_path / "league.db"
    database = Database(path)
    connection = database.connect()
    connection.execute("PRAGMA user_version=999")
    database.close()

    with pytest.raises(RuntimeError, match="written by a newer version"):
        Database(path).connect()


# --- Transactions and drafts (immutable) --------------------------------------


async def test_transactions_are_stored_once_and_re_archiving_is_a_no_op(db):
    transactions = [
        {"transaction_id": "t1", "type": "waiver", "status": "complete", "leg": 3,
         "created": 1_700_000_000_000, "status_updated": 1_700_000_100_000,
         "creator": "u1", "roster_ids": [1], "settings": {"waiver_bid": 14}},
        {"transaction_id": "t2", "type": "trade", "status": "complete", "leg": 4,
         "created": 1_700_100_000_000, "roster_ids": [1, 2]},
    ]
    await store.save_transactions(db, 2025, "L1", transactions)
    await store.save_transactions(db, 2025, "L1", transactions)

    assert db.stats()["rows"]["transactions"] == 2
    loaded = await store.load_transactions(db, seasons=[2025])
    assert {t["transaction_id"] for t in loaded} == {"t1", "t2"}
    # The full Sleeper payload survives, so the analysis code is unchanged.
    assert loaded[0]["settings"]["waiver_bid"] == 14


async def test_seasons_are_kept_apart_and_selectable(db):
    for season, tx_id in ((2024, "a"), (2025, "b"), (2026, "c")):
        await store.save_transactions(
            db, season, f"L{season}",
            [{"transaction_id": tx_id, "type": "waiver", "status": "complete",
              "created": 1, "roster_ids": [1]}],
        )
    assert len(await store.load_transactions(db, seasons=[2025])) == 1
    assert len(await store.load_transactions(db, seasons=[2024, 2026])) == 2
    # No filter means every season, which is the point of the archive.
    assert len(await store.load_transactions(db)) == 3


async def test_a_pending_claim_that_settles_is_updated_not_duplicated(db):
    pending = {"transaction_id": "t1", "type": "waiver", "status": "pending",
               "created": 1, "status_updated": 1, "roster_ids": [1],
               "settings": {"waiver_bid": 9}}
    await store.save_transactions(db, 2025, "L1", [pending])
    await store.save_transactions(
        db, 2025, "L1", [{**pending, "status": "complete", "status_updated": 2}]
    )

    assert db.stats()["rows"]["transactions"] == 1
    assert (await store.load_transactions(db))[0]["status"] == "complete"


async def test_draft_picks_round_trip(db):
    picks = [
        {"pick_no": 1, "round": 1, "roster_id": 3, "player_id": "4034",
         "metadata": {"position": "RB"}, "is_keeper": True},
        {"pick_no": 2, "round": 1, "roster_id": 5, "player_id": "6794",
         "metadata": {"position": "WR"}},
    ]
    await store.save_draft_picks(db, 2025, "d1", picks)
    await store.save_draft_picks(db, 2025, "d1", picks)

    loaded = await store.load_draft_picks(db, [2025])
    assert len(loaded) == 2
    assert loaded[0]["metadata"]["position"] == "RB"


# --- Append-only histories ----------------------------------------------------


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
    # The sequence is the history: both the opener and the move are kept.
    assert [r["home_spread"] for r in rows] == [-9.0, -6.5]


async def test_practice_participation_changes_are_each_recorded(db):
    for practice in ("did_not_practice", "limited", "limited", "full"):
        await store.append_injuries(db, 2025, 2, injury_rows("SF", _injuries(practice=practice)))

    rows = await store.read_injuries(db, 2025, week=2)
    assert [r["practice_participation"] for r in rows] == [
        "did_not_practice", "limited", "full",
    ]


async def test_histories_are_partitioned_by_week_and_season(db):
    await store.append_odds(db, 2025, 2, odds_rows(_odds()))
    await store.append_odds(db, 2025, 3, odds_rows(_odds(spread=-3.0)))
    await store.append_odds(db, 2026, 1, odds_rows(_odds(spread=-1.0)))

    assert len(await store.read_odds(db, 2025, week=2)) == 1
    assert len(await store.read_odds(db, 2025)) == 2
    assert len(await store.read_odds(db, 2026)) == 1


async def test_only_the_durable_fields_are_kept(db):
    game = parse_game(fx.ODDS_PAYLOAD[0])
    await store.append_odds(db, 2025, 2, odds_rows([game]))
    row = (await store.read_odds(db, 2025, week=2))[0]

    assert row["home_team"] == "KC"
    assert row["favourite"] == "KC"
    assert row["total"] == 50.0
    # The bookmaker blob is not worth keeping forever.
    assert "bookmakers" not in row


async def test_reading_a_season_with_nothing_archived_is_empty_not_an_error(db):
    assert await store.read_odds(db, 1999) == []
    assert await store.read_injuries(db, 1999) == []
    assert await store.read_decisions(db, 1999) == []


# --- Decision log -------------------------------------------------------------


async def test_an_outcome_is_layered_on_without_editing_the_original(db):
    decision = decision_row(
        kind="waiver_bid", summary="Bid 14", reasoning="His max ever is 12"
    )
    await store.append_decision(db, 2025, 2, decision)
    original = await store.find_decision(db, 2025, decision["decision_id"])
    await store.append_decision(db, 2025, 2, outcome_row(original, "Won it at 14"))

    # Two rows on disk, one decision when read back.
    assert db.stats()["rows"]["decisions"] == 2
    rows = await store.read_decisions(db, 2025)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "Won it at 14"
    # The reasoning as written at the time survives untouched.
    assert rows[0]["reasoning"] == "His max ever is 12"


async def test_decisions_are_always_appended_even_when_identical(db):
    """A log is not a state snapshot: making the same call twice is itself a
    fact worth keeping."""
    for _ in range(2):
        await store.append_decision(
            db, 2025, 2, decision_row(kind="start_sit", summary="Sat Kelce")
        )
    assert len(await store.read_decisions(db, 2025)) == 2


async def test_a_missing_decision_is_reported_as_missing(db):
    assert await store.find_decision(db, 2025, "nope") is None


# --- Inventory ----------------------------------------------------------------


async def test_inventory_reports_what_is_archived(db):
    await store.append_odds(db, 2025, 2, odds_rows(_odds()))
    await store.append_injuries(db, 2025, 2, injury_rows("SF", _injuries()))
    await store.save_transactions(
        db, 2024, "L1",
        [{"transaction_id": "t", "type": "waiver", "status": "complete",
          "created": 1, "leg": 5, "roster_ids": [1]}],
    )

    inventory = await store.inventory(db)
    assert inventory["odds"]["2025"]["rows"] == 1
    assert inventory["odds"]["2025"]["weeks"] == [2]
    assert inventory["injuries"]["2025"]["rows"] == 1
    assert inventory["transactions"]["2024"]["weeks"] == [5]
    assert inventory["decisions"] == {}


# --- capture_week -------------------------------------------------------------


class _FakeCache:
    def __init__(self, data):
        self._data = data

    async def get_or_refresh(self, key, loader, ttl=None):
        return self._data, {}


class _FakeOdds:
    configured = True

    def __init__(self, games):
        self.cache = _FakeCache({"games": games})

    async def _fetch(self):  # pragma: no cover - reached through the fake cache
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
        season=2025,
        week=2,
        teams=["SF", "KC"],
    )
    assert result["odds"]["written"] == 1
    assert result["injuries"]["written"] == 1
    assert result["injuries"]["teams_captured"] == 2


async def test_capture_survives_one_team_failing(db):
    result = await capture_week(
        db,
        odds_provider=_FakeOdds([parse_game(fx.ODDS_PAYLOAD[0])]),
        espn_provider=_FakeEspn({"SF": _injuries(), "KC": RuntimeError("ESPN 500")}),
        season=2025,
        week=2,
        teams=["SF", "KC"],
    )
    # SF is still archived even though KC blew up.
    assert result["injuries"]["written"] == 1
    assert result["injuries"]["teams_captured"] == 1
    assert "KC: ESPN 500" in result["injuries"]["teams_failed"]
    assert result["odds"]["written"] == 1


async def test_capture_without_an_odds_key_still_archives_injuries(db):
    class Unconfigured:
        configured = False

    result = await capture_week(
        db,
        odds_provider=Unconfigured(),
        espn_provider=_FakeEspn({"SF": _injuries()}),
        season=2025,
        week=2,
        teams=["SF"],
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

    # A broken archive must not propagate out of a read request.
    await auto_capture(Broken(), "odds", 2025, 2, odds_rows(_odds()))
    await auto_capture(db, "odds", None, 2, odds_rows(_odds()))
    await auto_capture(db, "odds", 2025, 2, [])
