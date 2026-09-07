"""HTTP integration tests for GET /leagues/{league}/briefing/{manager}."""

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

    async def fake_state(league_id=None):
        return {"week": 5, "season": "2025"}

    async def fake_league(league_id):
        return fixtures.LEAGUE

    async def fake_users(league_id):
        return fixtures.USERS

    async def fake_rosters(league_id):
        return fixtures.ROSTERS

    async def fake_public_get(path, params=None):
        if path == "/injury-report/4034":
            # Sleeper has McCaffrey "Questionable"; ESPN says "Doubtful" - a
            # real disagreement worth surfacing.
            return {
                "listed": True,
                "sleeper_injury_status": "Questionable",
                "espn_report": {"status": "Doubtful", "practice_participation": "limited"},
            }
        if path.startswith("/injury-report/"):
            return {"listed": False}
        if path == "/byes/2025":
            # SF (McCaffrey's team) is on bye next week.
            return {"byes": {"SF": 6, "KC": 9}}
        if path == "/weather/5":
            return {
                "available": True,
                "week": 5,
                "games": [],
                "outdoor_games_with_concerns": [
                    {"game": "KC @ BUF", "home_team": "BUF", "away_team": "KC"},
                    {"game": "DAL @ PHI", "home_team": "PHI", "away_team": "DAL"},
                ],
            }
        raise AssertionError(f"unexpected public.get path: {path}")

    async def fake_public_post(path, params=None, json=None):
        return {"scoring_not_applied": [], "players": []}

    monkeypatch.setattr(main.client, "nfl_state", fake_state)
    monkeypatch.setattr(main.client, "league", fake_league)
    monkeypatch.setattr(main.client, "users", fake_users)
    monkeypatch.setattr(main.client, "rosters", fake_rosters)
    monkeypatch.setattr(main.public, "get", fake_public_get)
    monkeypatch.setattr(main.public, "post", fake_public_post)

    with TestClient(main.app) as c:
        yield c


def test_the_briefing_merges_every_block(client):
    response = client.get("/leagues/main/briefing/elchubi", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()

    assert body["week"] == 5
    disagreement = body["injury_disagreements"][0]
    assert disagreement["sleeper_injury_status"] == "Questionable"
    assert disagreement["espn_report"]["status"] == "Doubtful"

    byes = {b["nfl_team"] for b in body["upcoming_byes"]}
    assert "SF" in byes  # bye week 6, within the 2-week "soon" window from week 5

    # Roster 1 has players on both KC and DAL (fixtures.ROSTERS[0]), so both
    # weather concerns should surface - any team with a rostered player
    # counts, IR-listed players included, since they may return.
    weather_games = {g["game"] for g in body["weather_concerns"]}
    assert weather_games == {"KC @ BUF", "DAL @ PHI"}

    assert isinstance(body["thin_positions"], list)
    assert isinstance(body["trending_free_agents"], dict)


def test_404s_on_an_unmatched_manager(client):
    response = client.get("/leagues/main/briefing/nobody-here", headers=HEADERS)
    assert response.status_code == 404


def test_a_failing_block_reports_itself_rather_than_failing_the_whole_briefing(
    client, monkeypatch
):
    import main

    async def failing_get(path, params=None):
        from fastapi import HTTPException

        raise HTTPException(status_code=502, detail="public-data is down")

    monkeypatch.setattr(main.public, "get", failing_get)
    response = client.get("/leagues/main/briefing/elchubi", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["injury_disagreements"] == []  # gather(..., return_exceptions=True) inside
    assert "error" in body["upcoming_byes"]
    assert "error" in body["weather_concerns"]


def test_requires_the_api_key(client):
    assert client.get("/leagues/main/briefing/elchubi").status_code == 401
