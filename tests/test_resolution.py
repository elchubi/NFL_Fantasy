"""Offline checks for the resolution layer, using fixture payloads."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LEAGUES", "main:1390746710426255360")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import services  # noqa: E402
from app.players import PlayerStore, _slim  # noqa: E402
from tests import fixtures  # noqa: E402


@pytest.fixture
def players(tmp_path):
    store = PlayerStore.__new__(PlayerStore)
    store._players = {pid: _slim(pid, raw) for pid, raw in fixtures.PLAYERS_RAW.items()}
    store._fetched_at = time.time()
    store._path = tmp_path / "players_cache.json"
    store._ttl_seconds = 20 * 3600
    return store


@pytest.fixture
def teams():
    return services.build_teams(fixtures.USERS, fixtures.ROSTERS)


def test_player_names_are_resolved(players):
    assert players.resolve("4046")["name"] == "Patrick Mahomes"
    # full_name is missing from Sleeper for many players; it gets rebuilt.
    assert players.resolve("4034")["name"] == "Christian McCaffrey"
    assert players.resolve("4034")["injury_status"] == "Questionable"
    # Team defenses are keyed by team abbreviation.
    assert players.resolve("KC")["position"] == "DEF"


def test_unknown_player_does_not_blow_up(players):
    unknown = players.resolve("does-not-exist")
    assert unknown["resolved"] is False
    assert "does-not-exist" in unknown["name"]


def test_starters_are_split_by_lineup_slot(players, teams):
    resolved = services.resolve_roster(
        fixtures.ROSTERS[0], teams[1], fixtures.LEAGUE["roster_positions"], players
    )
    slots = [s["slot"] for s in resolved["starters"]]
    assert slots == ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF"]
    assert resolved["starters"][0]["player"]["name"] == "Patrick Mahomes"
    # "0" in the starters array means an empty slot, not a player.
    assert resolved["starters"][2]["empty"] is True
    assert resolved["starters"][7]["player"] is None
    # Bench excludes starters and IR.
    bench_names = [p["name"] for p in resolved["bench"]]
    assert bench_names == ["Bench Guy"]
    assert [p["name"] for p in resolved["injured_reserve"]] == ["Hurt Player"]


def test_team_identity_and_record(teams):
    assert teams[1]["display_name"] == "elchubi"
    assert teams[1]["team_name"] == "Los Tacos Voladores"
    assert teams[1]["record"]["points_for"] == 512.34


def test_flexible_manager_lookup(teams):
    for query in ("elchubi", "ELCHUBI", "Los Tacos Voladores", "los tacos", "  tacos "):
        match, _ = services.find_team(teams, query)
        assert match is not None and match["roster_id"] == 1, query

    missing, candidates = services.find_team(teams, "nobody-here")
    assert missing is None and candidates == []


def test_matchups_are_paired_with_a_leader(players, teams):
    matchups = services.build_matchups(
        fixtures.MATCHUPS, teams, fixtures.LEAGUE["roster_positions"], players
    )
    assert len(matchups) == 1
    matchup = matchups[0]
    assert len(matchup["teams"]) == 2
    assert matchup["leader"]["team_name"] == "Los Tacos Voladores"
    assert matchup["leader"]["margin"] == 12.3
    assert matchup["teams"][0]["starters"][0]["player"]["name"] == "Patrick Mahomes"
    assert matchup["teams"][0]["starters"][0]["points"] == 25.1


def test_transactions_are_resolved_and_windowed(players, teams):
    now_ms = time.time() * 1000
    raw = [
        {
            "transaction_id": "t1",
            "type": "waiver",
            "status": "complete",
            "leg": 5,
            "created": now_ms - 3600_000,
            "status_updated": now_ms - 3600_000,
            "creator": "u1",
            "roster_ids": [1],
            "adds": {"9999": 1},
            "drops": {"8888": 1},
            "settings": {"waiver_bid": 14},
        },
        {
            "transaction_id": "old",
            "type": "free_agent",
            "status": "complete",
            "created": now_ms - 30 * 86400_000,
            "status_updated": now_ms - 30 * 86400_000,
            "creator": "u2",
            "roster_ids": [2],
            "adds": {"6794": 2},
            "drops": {},
        },
    ]
    users_by_id = {u["user_id"]: u for u in fixtures.USERS}
    since = (time.time() - 7 * 86400) * 1000

    result = services.build_transactions(raw, teams, players, users_by_id, since)
    assert [t["transaction_id"] for t in result] == ["t1"]
    tx = result[0]
    assert tx["type"] == "waiver"
    assert tx["adds"][0]["player"]["name"] == "Bench Guy"
    assert tx["adds"][0]["to_team"]["team_name"] == "Los Tacos Voladores"
    assert tx["waiver_bid"] == 14
    assert "$14 FAAB" in tx["summary"]


def test_weeks_to_scan_covers_the_window():
    assert services._weeks_to_scan(10, 7) == [10, 9, 8]
    assert services._weeks_to_scan(2, 30) == [2, 1]


def test_league_settings_are_human_readable():
    view = services.league_settings_view(fixtures.LEAGUE)
    assert view["format"]["type"] == "Keeper"
    assert view["format"]["max_keepers"] == 1
    assert view["scoring"]["ppr"].startswith("Full PPR")
    assert view["roster"]["starters"] == 10
    assert view["roster"]["bench_slots"] == 6
    assert view["playoffs"]["teams_in_playoffs"] == 6
    assert view["trades"]["deadline"] == "End of week 13"
    assert view["waivers"]["type"] == "FAAB blind bidding ($100 season budget)"
    assert view["waivers"]["process_day"] == "Tuesday"

    passing = {item["key"]: item for item in view["scoring"]["settings_by_group"]["passing"]}
    assert passing["pass_td"]["description"] == "Points per passing touchdown"
    # Unknown keys still get a sensible description instead of being dropped.
    other = {item["key"] for item in view["scoring"]["settings_by_group"]["other"]}
    assert "some_future_key" in other


def test_health_is_open_and_endpoints_require_the_api_key():
    import main

    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/leagues/main/snapshot").status_code == 401
        assert (
            client.get("/leagues/main/snapshot", headers={"X-API-Key": "wrong"}).status_code
            == 401
        )
        assert client.get("/leagues/main/roster/elchubi").status_code == 401
