"""HTTP integration tests for GET /leagues/{league}/playoff-odds.

Exercises real routing through main.app with the Sleeper client monkeypatched,
since the actual upstream is unreachable in this environment.
"""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LEAGUES", "main:1390746710426255360")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from tests import fixtures  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}


def _client(monkeypatch, *, playoff_teams, matchups_by_week, rosters=None):
    import main

    league = copy.deepcopy(fixtures.LEAGUE)
    league["settings"]["playoff_teams"] = playoff_teams
    league["settings"]["playoff_week_start"] = 5
    rosters = rosters if rosters is not None else fixtures.ROSTERS

    async def fake_state(league_id=None):
        return {"week": 4, "season": "2025"}

    async def fake_league(league_id):
        return league

    async def fake_users(league_id):
        return fixtures.USERS

    async def fake_rosters(league_id):
        return rosters

    async def fake_matchups(league_id, week):
        return matchups_by_week.get(week, [])

    monkeypatch.setattr(main.client, "nfl_state", fake_state)
    monkeypatch.setattr(main.client, "league", fake_league)
    monkeypatch.setattr(main.client, "users", fake_users)
    monkeypatch.setattr(main.client, "rosters", fake_rosters)
    monkeypatch.setattr(main.client, "matchups", fake_matchups)

    return TestClient(main.app)


def test_every_team_qualifies_when_there_are_more_spots_than_teams(monkeypatch):
    """fixtures.ROSTERS has 2 teams; 6 playoff spots means both always make it,
    same as it would be for real with such a small league."""
    history = {
        1: [{"roster_id": 1, "points": 110.0}, {"roster_id": 2, "points": 90.0}],
        2: [{"roster_id": 1, "points": 105.0}, {"roster_id": 2, "points": 95.0}],
        3: [{"roster_id": 1, "points": 108.0}, {"roster_id": 2, "points": 100.0}],
    }
    remaining = {4: [{"roster_id": 1, "matchup_id": 1}, {"roster_id": 2, "matchup_id": 1}]}
    client = _client(monkeypatch, playoff_teams=6, matchups_by_week={**history, **remaining})

    with client as c:
        response = c.get("/leagues/main/playoff-odds?trials=200", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["playoff_spots"] == 6
    assert body["weeks_simulated"] == [4]
    for team in body["teams"]:
        assert team["playoff_odds"] == 1.0
        assert team["read"] == "buyer"


def test_the_stronger_record_and_higher_scorer_leads_the_sort(monkeypatch):
    """Roster 1 both leads on wins (current standings, straight from Sleeper's
    own roster.settings - not recomputed here) and has been crushing its
    opponent every week, so it should take the league's only playoff spot
    almost every simulated trial."""
    rosters = copy.deepcopy(fixtures.ROSTERS)
    rosters[0]["settings"]["wins"], rosters[0]["settings"]["losses"] = 6, 0
    rosters[1]["settings"]["wins"], rosters[1]["settings"]["losses"] = 0, 6

    history = {
        1: [{"roster_id": 1, "points": 150.0}, {"roster_id": 2, "points": 80.0}],
        2: [{"roster_id": 1, "points": 145.0}, {"roster_id": 2, "points": 85.0}],
        3: [{"roster_id": 1, "points": 155.0}, {"roster_id": 2, "points": 82.0}],
    }
    remaining = {4: [{"roster_id": 1, "matchup_id": 1}, {"roster_id": 2, "matchup_id": 1}]}
    client = _client(
        monkeypatch,
        playoff_teams=1,
        matchups_by_week={**history, **remaining},
        rosters=rosters,
    )

    with client as c:
        response = c.get("/leagues/main/playoff-odds?trials=500", headers=HEADERS)
    body = response.json()
    # Only one spot: the team that has been crushing its opponent every week
    # and already leads on record should take nearly all of it.
    assert body["teams"][0]["roster_id"] == 1
    assert body["teams"][0]["playoff_odds"] > body["teams"][1]["playoff_odds"]


def test_a_missing_future_week_does_not_break_the_simulation(monkeypatch):
    """Sleeper returns [] for a week it has not generated matchups for yet -
    the simulation should just treat that week as a bye rather than error."""
    client = _client(monkeypatch, playoff_teams=6, matchups_by_week={})
    with client as c:
        response = c.get("/leagues/main/playoff-odds?trials=100", headers=HEADERS)
    assert response.status_code == 200


def test_requires_the_api_key(monkeypatch):
    client = _client(monkeypatch, playoff_teams=6, matchups_by_week={})
    with client as c:
        assert c.get("/leagues/main/playoff-odds").status_code == 401
