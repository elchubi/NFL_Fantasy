"""HTTP-level tests that two leagues on one backend process stay genuinely
separate: different Sleeper data, different SQLite databases, no leakage.

Earlier verification of this was done by hand with real subprocesses (see the
session history) rather than as an automated test - this closes that gap.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LEAGUES", "main:1390746710426255360")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Database  # noqa: E402
from app.players import _slim  # noqa: E402
from tests import fixtures  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}

# Two distinct Sleeper league ids, standing in for two real leagues on one process.
MAIN_LEAGUE_ID = "1001"
DYNASTY_LEAGUE_ID = "2002"

LEAGUE_BY_ID = {
    MAIN_LEAGUE_ID: {**fixtures.LEAGUE, "league_id": MAIN_LEAGUE_ID, "name": "Main League"},
    DYNASTY_LEAGUE_ID: {**fixtures.LEAGUE, "league_id": DYNASTY_LEAGUE_ID, "name": "Dynasty League"},
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    import main

    # Reconfigure the already-imported singletons for two leagues, the same
    # shape LEAGUES=main:...,dynasty:... produces at real startup - a second
    # slug pointing at a second Sleeper league id.
    monkeypatch.setattr(
        main.settings, "leagues", f"main:{MAIN_LEAGUE_ID},dynasty:{DYNASTY_LEAGUE_ID}"
    )
    main.databases["dynasty"] = Database(tmp_path / "league-dynasty.db")
    # main's own database file would otherwise be whatever CACHE_DIR/.env.example
    # resolved to at import time - redirect it into this test's tmp_path too, so
    # runs never share state with each other or with a stray local file.
    main.databases["main"] = Database(tmp_path / "league-main.db")

    monkeypatch.setattr(main.players, "_path", tmp_path / "no-such-cache.json")
    main.players._players = {
        pid: _slim(pid, raw) for pid, raw in fixtures.PLAYERS_RAW.items()
    }
    main.players._reindex()
    main.players._fetched_at = time.time()

    async def fake_state(league_id=None):
        return {"week": 5, "season": "2025"}

    async def fake_league(league_id):
        return LEAGUE_BY_ID[league_id]

    async def fake_users(league_id):
        return fixtures.USERS

    async def fake_rosters(league_id):
        return fixtures.ROSTERS

    async def fake_public_get(path, params=None):
        return {"archived": {}}

    monkeypatch.setattr(main.client, "nfl_state", fake_state)
    monkeypatch.setattr(main.client, "league", fake_league)
    monkeypatch.setattr(main.client, "users", fake_users)
    monkeypatch.setattr(main.client, "rosters", fake_rosters)
    monkeypatch.setattr(main.public, "get", fake_public_get)

    with TestClient(main.app) as c:
        yield c


def test_get_leagues_lists_both_slugs(client):
    response = client.get("/leagues", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    by_slug = {entry["slug"]: entry["league_id"] for entry in body["leagues"]}
    assert by_slug == {"main": MAIN_LEAGUE_ID, "dynasty": DYNASTY_LEAGUE_ID}


def test_each_slug_resolves_to_its_own_sleeper_league(client):
    main_settings = client.get("/leagues/main/league-settings", headers=HEADERS).json()
    dynasty_settings = client.get(
        "/leagues/dynasty/league-settings", headers=HEADERS
    ).json()
    assert main_settings["league_id"] == MAIN_LEAGUE_ID
    assert dynasty_settings["league_id"] == DYNASTY_LEAGUE_ID
    assert main_settings["name"] == "Main League"
    assert dynasty_settings["name"] == "Dynasty League"


def test_an_unconfigured_slug_404s_and_lists_the_real_ones(client):
    response = client.get("/leagues/thirdleague/snapshot", headers=HEADERS)
    assert response.status_code == 404
    assert "main" in response.json()["detail"] and "dynasty" in response.json()["detail"]


def test_decisions_logged_on_one_league_never_appear_on_the_other(client):
    logged = client.post(
        "/leagues/main/decision?kind=trade&summary=only+for+main", headers=HEADERS
    )
    assert logged.status_code == 200

    main_decisions = client.get("/leagues/main/decisions", headers=HEADERS).json()
    dynasty_decisions = client.get("/leagues/dynasty/decisions", headers=HEADERS).json()

    assert main_decisions["count"] == 1
    assert dynasty_decisions["count"] == 0


def test_the_two_leagues_use_genuinely_different_database_files(client):
    import main

    assert main.databases["main"].path != main.databases["dynasty"].path
    main_history = client.get("/leagues/main/history", headers=HEADERS).json()
    dynasty_history = client.get("/leagues/dynasty/history", headers=HEADERS).json()
    assert main_history["database"]["path"] != dynasty_history["database"]["path"]


def test_requires_the_api_key(client):
    assert client.get("/leagues").status_code == 401
