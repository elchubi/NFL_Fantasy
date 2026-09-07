"""HTTP integration tests for GET /leagues/{league}/faab-bid/{manager}."""

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

    # Roster 2 (rival99) has 60 used of a 100 budget, so 40 remaining - a
    # real threat with room to actually outbid.
    rosters = copy.deepcopy(fixtures.ROSTERS)
    rosters[0]["settings"]["waiver_budget_used"] = 25
    rosters[1]["settings"]["waiver_budget_used"] = 60

    async def fake_state(league_id=None):
        return {"week": 5, "season": "2025"}

    async def fake_league(league_id):
        return fixtures.LEAGUE

    async def fake_users(league_id):
        return fixtures.USERS

    async def fake_rosters(league_id):
        return rosters

    async def fake_transactions(league_id, week):
        # rival99 (roster_id 2) has bid aggressively before: a real rival.
        if week == 1:
            return [
                {
                    "type": "waiver", "status": "complete", "roster_ids": [2],
                    "status_updated": 1_700_000_000_000, "adds": {}, "drops": {},
                    "settings": {"waiver_bid": 45},
                },
                {
                    "type": "waiver", "status": "complete", "roster_ids": [2],
                    "status_updated": 1_700_000_100_000, "adds": {}, "drops": {},
                    "settings": {"waiver_bid": 35},
                },
            ]
        return []

    async def fake_drafts(league_id):
        return []

    async def fake_public_get(path, params=None):
        return {"rows": []}

    monkeypatch.setattr(main.client, "nfl_state", fake_state)
    monkeypatch.setattr(main.client, "league", fake_league)
    monkeypatch.setattr(main.client, "users", fake_users)
    monkeypatch.setattr(main.client, "rosters", fake_rosters)
    monkeypatch.setattr(main.client, "transactions", fake_transactions)
    monkeypatch.setattr(main.client, "drafts", fake_drafts)
    monkeypatch.setattr(main.public, "get", fake_public_get)

    with TestClient(main.app) as c:
        yield c


def test_the_recommendation_is_anchored_to_the_most_dangerous_rival(client):
    response = client.get(
        "/leagues/main/faab-bid/elchubi?player_id=9999&confidence=medium", headers=HEADERS
    )
    assert response.status_code == 200
    body = response.json()

    assert body["your_remaining_budget"] == 75  # 100 - 25
    assert body["top_rival"]["display_name"] == "Rival99"
    assert body["top_rival"]["max_bid_ever"] == 45
    assert body["top_rival"]["remaining_budget"] == 40
    # 45 * 1.15 = 51.75 -> 52, comfortably under elchubi's 75 remaining.
    assert body["recommended_bid"] == 52
    assert body["affordable"] is True
    assert body["player"]["name"] == "Bench Guy"


def test_higher_confidence_recommends_a_bigger_bid(client):
    medium = client.get(
        "/leagues/main/faab-bid/elchubi?confidence=medium", headers=HEADERS
    ).json()
    high = client.get(
        "/leagues/main/faab-bid/elchubi?confidence=high", headers=HEADERS
    ).json()
    assert high["recommended_bid"] > medium["recommended_bid"]


def test_the_recommendation_never_exceeds_your_own_remaining_budget(client, monkeypatch):
    import main

    async def poor_rosters(league_id):
        rosters = copy.deepcopy(fixtures.ROSTERS)
        rosters[0]["settings"]["waiver_budget_used"] = 97  # only $3 left
        rosters[1]["settings"]["waiver_budget_used"] = 0
        return rosters

    monkeypatch.setattr(main.client, "rosters", poor_rosters)
    response = client.get(
        "/leagues/main/faab-bid/elchubi?confidence=high", headers=HEADERS
    )
    body = response.json()
    assert body["recommended_bid"] <= body["your_remaining_budget"]


def test_an_invalid_confidence_is_rejected(client):
    response = client.get(
        "/leagues/main/faab-bid/elchubi?confidence=extreme", headers=HEADERS
    )
    assert response.status_code == 400


def test_requires_the_api_key(client):
    assert client.get("/leagues/main/faab-bid/elchubi").status_code == 401
