"""HTTP integration tests for GET /leagues/{league}/draft-picks/{manager}
and GET /leagues/{league}/draft-board.

Exercises real routing through main.app with the Sleeper client monkeypatched
and a real tmp-path SQLite database, since the actual upstreams are
unreachable in this environment - same pattern as test_multi_league.py.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LEAGUES", "main:1390746710426255360")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import store  # noqa: E402
from app.db import Database  # noqa: E402
from app.players import _slim  # noqa: E402
from tests import fixtures  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}

# fixtures.ROSTERS has roster 1 (elchubi) and roster 2 (Rival99).
PICKS_2025 = [
    {"pick_no": 1, "round": 1, "roster_id": 1, "player_id": "4034",
     "metadata": {"position": "RB"}},
    {"pick_no": 3, "round": 1, "roster_id": 2, "player_id": "6794",
     "metadata": {"position": "WR"}},
    {"pick_no": 13, "round": 2, "roster_id": 1, "player_id": "1466",
     "metadata": {"position": "TE"}},
]


@pytest.fixture
def client(monkeypatch, tmp_path):
    import main

    monkeypatch.setattr(main.players, "_path", tmp_path / "no-such-cache.json")
    main.players._players = {
        pid: _slim(pid, raw) for pid, raw in fixtures.PLAYERS_RAW.items()
    }
    main.players._reindex()
    main.players._fetched_at = time.time()

    # Redirect 'main' into this test's tmp_path so runs never share state
    # with each other or with a stray local file (same reasoning as
    # test_multi_league.py).
    main.databases["main"] = Database(tmp_path / "league-main.db")

    async def fake_league(league_id):
        return fixtures.LEAGUE

    async def fake_users(league_id):
        return fixtures.USERS

    async def fake_rosters(league_id):
        return fixtures.ROSTERS

    monkeypatch.setattr(main.client, "league", fake_league)
    monkeypatch.setattr(main.client, "users", fake_users)
    monkeypatch.setattr(main.client, "rosters", fake_rosters)

    with TestClient(main.app) as c:
        yield c


async def test_a_managers_picks_come_back_in_round_order(client):
    import main

    await store.save_draft_picks(main.databases["main"], 2025, "d1", PICKS_2025)

    response = client.get("/leagues/main/draft-picks/elchubi?season=2025", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "archive"
    assert body["season"] == 2025
    assert body["team"]["display_name"] == "elchubi"
    # Round 1 pick before round 2, not the raw archive insertion order.
    assert [p["pick_no"] for p in body["picks"]] == [1, 13]
    assert body["picks"][0]["player"]["name"] == "Christian McCaffrey"
    assert body["picks"][0]["round"] == 1
    # Rival99's pick (pick_no 3) must never show up in elchubi's list.
    assert all(p["pick_no"] != 3 for p in body["picks"])


async def test_draft_board_lists_every_team_in_overall_pick_order(client):
    import main

    await store.save_draft_picks(main.databases["main"], 2025, "d1", PICKS_2025)

    response = client.get("/leagues/main/draft-board?season=2025", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "archive"
    assert [p["pick_no"] for p in body["picks"]] == [1, 3, 13]
    first, second = body["picks"][0], body["picks"][1]
    assert first["team"]["display_name"] == "elchubi"
    assert first["player"]["name"] == "Christian McCaffrey"
    assert second["team"]["display_name"] == "Rival99"
    assert second["player"]["name"] == "Justin Jefferson"


def test_falls_back_to_the_live_draft_when_nothing_is_archived(client, monkeypatch):
    import main

    async def fake_drafts(league_id):
        return [{"draft_id": "d1", "season": "2025"}]

    async def fake_draft_picks(draft_id):
        assert draft_id == "d1"
        return PICKS_2025

    monkeypatch.setattr(main.client, "drafts", fake_drafts)
    monkeypatch.setattr(main.client, "draft_picks", fake_draft_picks)

    response = client.get("/leagues/main/draft-picks/elchubi", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "live (current season only)"
    assert [p["pick_no"] for p in body["picks"]] == [1, 13]


def test_an_unmatched_manager_404s(client):
    response = client.get("/leagues/main/draft-picks/nobody-here", headers=HEADERS)
    assert response.status_code == 404


def test_draft_picks_requires_the_api_key(client):
    assert client.get("/leagues/main/draft-picks/elchubi").status_code == 401


def test_draft_board_requires_the_api_key(client):
    assert client.get("/leagues/main/draft-board").status_code == 401
