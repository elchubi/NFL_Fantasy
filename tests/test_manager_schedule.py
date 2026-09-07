"""HTTP integration tests for GET /leagues/{league}/schedule/{manager}.

Exercises real routing through main.app with the Sleeper client monkeypatched,
since the actual upstream is unreachable in this environment - same pattern as
test_playoff_odds.py, which already relies on client.matchups() returning
real roster_id/matchup_id pairs for future weeks, not just played ones.
"""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LEAGUES", "main:1390746710426255360")
os.environ.setdefault("API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

from tests import fixtures  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}


def _client(monkeypatch, *, matchups_by_week, playoff_week_start=5):
    import main

    league = copy.deepcopy(fixtures.LEAGUE)
    league["settings"]["playoff_week_start"] = playoff_week_start

    async def fake_league(league_id):
        return league

    async def fake_users(league_id):
        return fixtures.USERS

    async def fake_rosters(league_id):
        return fixtures.ROSTERS

    async def fake_matchups(league_id, week):
        return matchups_by_week.get(week, [])

    monkeypatch.setattr(main.client, "league", fake_league)
    monkeypatch.setattr(main.client, "users", fake_users)
    monkeypatch.setattr(main.client, "rosters", fake_rosters)
    monkeypatch.setattr(main.client, "matchups", fake_matchups)

    return TestClient(main.app)


def test_each_weeks_opponent_is_resolved_by_matchup_id(monkeypatch):
    # fixtures.ROSTERS has roster 1 (elchubi) and roster 2 (Rival99), paired
    # every week - a future week's pairing must resolve the same way a
    # played one does, since /playoff-odds already depends on that being true.
    matchups = {
        1: [{"roster_id": 1, "matchup_id": 1}, {"roster_id": 2, "matchup_id": 1}],
        2: [{"roster_id": 1, "matchup_id": 1}, {"roster_id": 2, "matchup_id": 1}],
        3: [{"roster_id": 1, "matchup_id": 1}, {"roster_id": 2, "matchup_id": 1}],
        4: [{"roster_id": 1, "matchup_id": 1}, {"roster_id": 2, "matchup_id": 1}],
    }
    client = _client(monkeypatch, matchups_by_week=matchups, playoff_week_start=5)

    with client as c:
        response = c.get("/leagues/main/schedule/elchubi", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["playoff_week_start"] == 5
    assert body["team"]["display_name"] == "elchubi"
    assert [w["week"] for w in body["schedule"]] == [1, 2, 3, 4]
    for week in body["schedule"]:
        assert week["bye"] is False
        assert week["opponent"]["display_name"] == "Rival99"
        assert week["opponent"]["team_name"] == "Gridiron Goats"


def test_a_week_with_no_matchup_data_yet_is_a_bye_not_an_error(monkeypatch):
    """Sleeper returns [] for a week it has not generated pairings for yet -
    same shape /playoff-odds already treats as a bye rather than erroring."""
    client = _client(monkeypatch, matchups_by_week={1: []}, playoff_week_start=2)

    with client as c:
        response = c.get("/leagues/main/schedule/elchubi", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["schedule"] == [{"week": 1, "bye": True, "opponent": None}]


def test_an_unmatched_manager_404s(monkeypatch):
    client = _client(monkeypatch, matchups_by_week={})
    with client as c:
        response = c.get("/leagues/main/schedule/nobody-here", headers=HEADERS)
    assert response.status_code == 404


def test_requires_the_api_key(monkeypatch):
    client = _client(monkeypatch, matchups_by_week={})
    with client as c:
        assert c.get("/leagues/main/schedule/elchubi").status_code == 401
