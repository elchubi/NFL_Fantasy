"""Offline tests for the append-only archive."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LEAGUE_ID", "1390746710426255360")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

from app.history import HistoryStore, capture_week, injury_rows, odds_rows  # noqa: E402
from app.odds import parse_game  # noqa: E402
from tests import fixtures_external as fx  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return HistoryStore(tmp_path / "history")


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


async def test_rows_are_appended_and_read_back(store):
    result = await store.append("odds", 2025, 2, odds_rows(_odds()))
    assert result["written"] == 1

    rows = store.read("odds", 2025, week=2)
    assert len(rows) == 1
    assert rows[0]["home_spread"] == -9.0
    assert rows[0]["season"] == 2025 and rows[0]["week"] == 2
    assert "captured_at" in rows[0]


async def test_an_unchanged_capture_writes_nothing(store):
    await store.append("odds", 2025, 2, odds_rows(_odds()))
    again = await store.append("odds", 2025, 2, odds_rows(_odds()))

    assert again["written"] == 0
    assert again["unchanged"] is True
    assert len(store.read("odds", 2025, week=2)) == 1


async def test_a_line_move_is_recorded_as_a_new_row(store):
    await store.append("odds", 2025, 2, odds_rows(_odds(spread=-9.0)))
    await store.append("odds", 2025, 2, odds_rows(_odds(spread=-6.5)))

    rows = store.read("odds", 2025, week=2)
    # The sequence is the history: both the opener and the move are kept.
    assert [r["home_spread"] for r in rows] == [-9.0, -6.5]


async def test_practice_participation_changes_are_each_recorded(store):
    await store.append("injuries", 2025, 2, injury_rows("SF", _injuries(practice="did_not_practice")))
    await store.append("injuries", 2025, 2, injury_rows("SF", _injuries(practice="limited")))
    await store.append("injuries", 2025, 2, injury_rows("SF", _injuries(practice="limited")))
    await store.append("injuries", 2025, 2, injury_rows("SF", _injuries(status="Active", practice="full")))

    rows = store.read("injuries", 2025, week=2)
    # Wednesday DNP -> Thursday limited -> (Friday unchanged) -> Friday full.
    assert [r["practice_participation"] for r in rows] == [
        "did_not_practice",
        "limited",
        "full",
    ]
    assert rows[-1]["status"] == "Active"


async def test_weeks_and_seasons_are_kept_separate(store):
    await store.append("odds", 2025, 2, odds_rows(_odds()))
    await store.append("odds", 2025, 3, odds_rows(_odds(spread=-3.0)))
    await store.append("odds", 2026, 1, odds_rows(_odds(spread=-1.0)))

    assert len(store.read("odds", 2025, week=2)) == 1
    assert len(store.read("odds", 2025)) == 2
    assert len(store.read("odds", 2026)) == 1
    # A different season is a different file.
    assert store.path_for("odds", 2025) != store.path_for("odds", 2026)


async def test_a_torn_line_does_not_break_the_archive(store, tmp_path):
    await store.append("odds", 2025, 2, odds_rows(_odds()))
    path = store.path_for("odds", 2025)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"season": 2025, "week": 2, "game_id": "tr')  # interrupted write
    await store.append("odds", 2025, 2, odds_rows(_odds(spread=-4.0)))

    rows = store.read("odds", 2025, week=2)
    assert [r["home_spread"] for r in rows] == [-9.0, -4.0]


async def test_inventory_reports_what_is_archived(store):
    await store.append("odds", 2025, 2, odds_rows(_odds()))
    await store.append("injuries", 2025, 2, injury_rows("SF", _injuries()))

    inventory = store.inventory()
    assert inventory["odds"]["2025"]["rows"] == 1
    assert inventory["odds"]["2025"]["weeks"] == [2]
    assert inventory["injuries"]["2025"]["rows"] == 1
    assert inventory["odds"]["2025"]["size_kb"] > 0


def test_reading_a_season_with_no_archive_is_empty_not_an_error(store):
    assert store.read("odds", 1999) == []
    assert store.inventory() == {"odds": {}, "injuries": {}, "decisions": {}}


def test_only_the_durable_fields_are_kept():
    game = parse_game(fx.ODDS_PAYLOAD[0])
    row = odds_rows([game])[0]
    # Abbreviations, not the long display names, and no bookmaker blob.
    assert row["home_team"] == "KC"
    assert row["favourite"] == "KC"
    assert "bookmakers" not in row
    assert row["total"] == 50.0


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
        team = key.split(":", 1)[1]
        outcome = self._per_team[team]
        if isinstance(outcome, Exception):
            raise outcome
        return {"injuries": outcome}, {}

    async def _fetch_team(self, team):  # pragma: no cover
        return {"injuries": []}


async def test_capture_week_writes_both_sources(store):
    result = await capture_week(
        store,
        odds_provider=_FakeOdds([parse_game(fx.ODDS_PAYLOAD[0])]),
        espn_provider=_FakeEspn({"SF": _injuries(), "KC": []}),
        season=2025,
        week=2,
        teams=["SF", "KC"],
    )
    assert result["odds"]["written"] == 1
    assert result["injuries"]["written"] == 1
    assert result["injuries"]["teams_captured"] == 2
    assert len(store.read("odds", 2025, week=2)) == 1


async def test_capture_survives_one_team_failing(store):
    result = await capture_week(
        store,
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


async def test_capture_without_an_odds_key_still_archives_injuries(store):
    class Unconfigured:
        configured = False

    result = await capture_week(
        store,
        odds_provider=Unconfigured(),
        espn_provider=_FakeEspn({"SF": _injuries()}),
        season=2025,
        week=2,
        teams=["SF"],
    )
    assert result["odds"]["written"] == 0
    assert "ODDS_API_KEY" in result["odds"]["error"]
    assert result["injuries"]["written"] == 1


# --- Auto-capture -------------------------------------------------------------


async def test_auto_capture_writes_and_never_raises(store):
    from app.history import auto_capture

    await auto_capture(store, "odds", 2025, 2, odds_rows(_odds()))
    assert len(store.read("odds", 2025, week=2)) == 1

    # A broken store must not propagate out of a read request.
    class Broken:
        async def append(self, *args, **kwargs):
            raise RuntimeError("disk on fire")

    await auto_capture(Broken(), "odds", 2025, 2, odds_rows(_odds()))


async def test_auto_capture_ignores_missing_season_or_week(store):
    from app.history import auto_capture

    await auto_capture(store, "odds", None, 2, odds_rows(_odds()))
    await auto_capture(store, "odds", 2025, None, odds_rows(_odds()))
    await auto_capture(store, "odds", 2025, 2, [])
    assert store.read("odds", 2025) == []


async def test_the_subject_index_survives_appends_without_rereading(store):
    """The dedupe memo must stay correct as rows are appended, since it is what
    keeps auto-capture off the archive file on every request."""
    await store.append("odds", 2025, 2, odds_rows(_odds(spread=-9.0)))
    # Force the memo to exist, then append through it.
    assert store._index[("odds", 2025, 2)]
    await store.append("odds", 2025, 2, odds_rows(_odds(spread=-9.0)))
    await store.append("odds", 2025, 2, odds_rows(_odds(spread=-6.0)))

    rows = store.read("odds", 2025, week=2)
    assert [r["home_spread"] for r in rows] == [-9.0, -6.0]

    # A fresh store reading the same file must reach the same conclusion.
    fresh = HistoryStore(store.directory)
    again = await fresh.append("odds", 2025, 2, odds_rows(_odds(spread=-6.0)))
    assert again["written"] == 0
