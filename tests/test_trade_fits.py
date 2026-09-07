"""HTTP integration tests for GET /leagues/{league}/trade-fits/{manager}.

Exercises real routing through main.app with the Sleeper client and the
public-data client monkeypatched, since the actual upstreams are unreachable
in this environment.
"""

import copy
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LEAGUES", "main:1390746710426255360")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.players import _slim  # noqa: E402
from tests import fixtures  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    import main

    monkeypatch.setattr(main.players, "_path", tmp_path / "no-such-cache.json")
    main.players._players = {
        pid: _slim(pid, raw) for pid, raw in fixtures.PLAYERS_RAW.items()
    }
    main.players._reindex()
    main.players._fetched_at = time.time()

    # A second, healthy WR for roster 2's surplus below - not in fixtures.PLAYERS_RAW.
    main.players._players["7005"] = _slim(
        "7005", {"first_name": "Depth", "last_name": "Receiver", "position": "WR", "team": "LAC"}
    )

    # Give roster 1 (elchubi) only one healthy WR against a need of two (WR
    # deficit); roster 2 (rival99) three healthy WRs against the same need
    # (a genuine +1 WR surplus it can trade away), and a weaker record/
    # scoring history so it reads as a seller.
    rosters = copy.deepcopy(fixtures.ROSTERS)
    rosters[0]["players"] = ["4046", "4034", "6794", "5849", "1466", "4098", "K1", "KC"]
    rosters[0]["starters"] = ["4046", "4034", "0", "6794", "5849", "1466", "4098", "0", "K1", "KC"]
    rosters[1]["players"] = [
        "4046", "4034", "6794", "5849", "1466", "4098", "K1", "KC", "9999", "7005",
    ]
    rosters[1]["settings"]["wins"], rosters[1]["settings"]["losses"] = 0, 6

    async def fake_state(league_id=None):
        return {"week": 5, "season": "2025"}

    league = copy.deepcopy(fixtures.LEAGUE)
    league["settings"]["playoff_teams"] = 1

    async def fake_league(league_id):
        return league

    async def fake_users(league_id):
        return fixtures.USERS

    async def fake_rosters(league_id):
        return rosters

    async def fake_matchups(league_id, week):
        return []

    async def fake_transactions(league_id, week):
        return []

    async def fake_drafts(league_id):
        return []

    async def fake_public_get(path, params=None):
        # managers() reads injury history from the shared public-data service;
        # it already tolerates that call failing, but mocking it avoids
        # depending on main.public's real (and, across the test suite,
        # possibly already-closed) httpx client at all.
        return {"rows": []}

    monkeypatch.setattr(main.client, "nfl_state", fake_state)
    monkeypatch.setattr(main.client, "league", fake_league)
    monkeypatch.setattr(main.client, "users", fake_users)
    monkeypatch.setattr(main.client, "rosters", fake_rosters)
    monkeypatch.setattr(main.client, "matchups", fake_matchups)
    monkeypatch.setattr(main.client, "transactions", fake_transactions)
    monkeypatch.setattr(main.client, "drafts", fake_drafts)
    monkeypatch.setattr(main.public, "get", fake_public_get)

    with TestClient(main.app) as c:
        yield c


def test_a_surplus_team_is_offered_as_a_candidate_for_your_deficit(client):
    response = client.get("/leagues/main/trade-fits/elchubi", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert "WR" in body["your_deficits"]

    candidates = {c["roster_id"]: c for c in body["candidates"]}
    assert 2 in candidates
    fits = {f["position"] for f in candidates[2]["positions_they_can_fill_for_you"]}
    assert "WR" in fits
    # 0-6 with a below-average scoring history: should read as a seller.
    assert candidates[2]["read"] == "seller"


def test_no_deficits_returns_an_empty_candidate_list_with_a_note(client, monkeypatch):
    import main

    # Even elchubi's full original roster is exactly at 0 spare everywhere
    # (RB/WR/TE all have precisely enough healthy bodies to fill their
    # dedicated slots, same as most real rosters - see test_edge.py's
    # "carrying exactly enough" case) - so give it genuine extra healthy
    # depth at every dedicated position to actually clear every deficit.
    main.players._players["7001"] = _slim(
        "7001", {"first_name": "Extra", "last_name": "QB", "position": "QB", "team": "LAC"}
    )
    main.players._players["7002"] = _slim(
        "7002", {"first_name": "Extra", "last_name": "RB", "position": "RB", "team": "LAC"}
    )
    main.players._players["7003"] = _slim(
        "7003", {"first_name": "Extra", "last_name": "WR", "position": "WR", "team": "LAC"}
    )
    main.players._players["7004"] = _slim(
        "7004", {"first_name": "Extra", "last_name": "TE", "position": "TE", "team": "LAC"}
    )

    async def fake_rosters(league_id):
        rosters = copy.deepcopy(fixtures.ROSTERS)
        rosters[0]["players"] = fixtures.ROSTERS[0]["players"] + [
            "7001", "7002", "7003", "7004",
        ]
        return rosters

    monkeypatch.setattr(main.client, "rosters", fake_rosters)
    response = client.get("/leagues/main/trade-fits/elchubi", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["your_deficits"] == []
    assert body["candidates"] == []
    assert "note" in body


def test_404s_on_an_unmatched_manager(client):
    response = client.get("/leagues/main/trade-fits/nobody-here", headers=HEADERS)
    assert response.status_code == 404


def test_requires_the_api_key(client):
    assert client.get("/leagues/main/trade-fits/elchubi").status_code == 401
