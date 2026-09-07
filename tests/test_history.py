"""Offline tests for the league's own SQLite archive: transactions, draft
picks and the decision log. Betting lines and injury reports are archived by
the shared public-data service now - see public_data/tests/test_history.py."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LEAGUE_ID", "1390746710426255360")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

from app import store  # noqa: E402
from app.db import Database  # noqa: E402
from app.history import decision_row, outcome_row  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "league.db")
    database.connect()
    yield database
    database.close()


# --- Schema -------------------------------------------------------------------


def test_migrations_run_once_and_are_idempotent(tmp_path):
    path = tmp_path / "league.db"
    first = Database(path)
    first.connect()
    version = first.schema_version
    assert version > 0
    first.close()

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


async def test_reading_a_season_with_nothing_archived_is_empty_not_an_error(db):
    assert await store.read_decisions(db, 1999) == []
    assert await store.load_transactions(db, seasons=[1999]) == []


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
    await store.save_transactions(
        db, 2024, "L1",
        [{"transaction_id": "t", "type": "waiver", "status": "complete",
          "created": 1, "leg": 5, "roster_ids": [1]}],
    )
    await store.append_decision(db, 2025, 2, decision_row(kind="trade", summary="x"))

    inventory = await store.inventory(db)
    assert inventory["transactions"]["2024"]["weeks"] == [5]
    assert inventory["decisions"]["2025"]["rows"] == 1
    # Odds/injuries are not this database's concern any more.
    assert "odds" not in inventory and "injuries" not in inventory
