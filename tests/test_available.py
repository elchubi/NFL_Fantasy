"""Integration tests for the free-agent pool endpoint: /leagues/{league}/available.

Exercises real HTTP routing through main.app with the Sleeper client and the
public-data client monkeypatched, since the actual upstreams are unreachable
in this environment - see README's "What was and wasn't verified".
"""

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

# A free agent nobody has rostered, resolvable by gsis_id.
FREE_AGENT_ID = "7777"
FREE_AGENT_GSIS = "00-0099999"
ROSTERED_RB_GSIS = "00-0011111"
# A long-retired player nflverse still has an old stat line for under this
# gsis_id, and who nobody has rostered either - but is not a real pickup.
RETIRED_ID = "8899"
RETIRED_GSIS = "00-0088888"


@pytest.fixture
def client(monkeypatch, tmp_path):
    import main

    # The lifespan reloads from main.players's real on-disk cache path on
    # startup; point it somewhere empty so a leftover local cache file from
    # unrelated manual testing can never leak into this test's player data.
    monkeypatch.setattr(main.players, "_path", tmp_path / "no-such-cache.json")

    main.players._players = {
        pid: _slim(pid, raw) for pid, raw in fixtures.PLAYERS_RAW.items()
    }
    main.players._players[FREE_AGENT_ID] = _slim(
        FREE_AGENT_ID,
        {
            "first_name": "Waiver", "last_name": "Wire", "position": "RB",
            "team": "NYJ", "status": "Active", "gsis_id": FREE_AGENT_GSIS,
        },
    )
    # 4034 (Christian McCaffrey) is on roster 1 in fixtures.ROSTERS.
    main.players._players["4034"]["gsis_id"] = ROSTERED_RB_GSIS
    main.players._players[RETIRED_ID] = _slim(
        RETIRED_ID,
        {
            "first_name": "Long", "last_name": "Retired", "position": "RB",
            "team": "IND", "status": None, "gsis_id": RETIRED_GSIS,
        },
    )
    main.players._reindex()
    main.players._fetched_at = time.time()

    async def fake_league(league_id):
        return fixtures.LEAGUE

    async def fake_rosters(league_id):
        return fixtures.ROSTERS

    async def fake_post(path, params=None, json=None):
        assert json is not None and "scoring_settings" in json
        if path == "/position-points/RB":
            return {
                "season": 2025,
                "position": "RB",
                "scoring_not_applied": ["idp_tkl"],
                "players": [
                    {
                        "gsis_id": FREE_AGENT_GSIS, "name": "Waiver Wire", "team": "NYJ",
                        "games": 3, "weekly_points": {"1": 10.0, "2": 12.0, "3": 14.0},
                        "season_total_points": 36.0, "season_average_points": 12.0,
                        "recent_average_points": 12.0,
                    },
                    {
                        "gsis_id": ROSTERED_RB_GSIS, "name": "Christian McCaffrey", "team": "SF",
                        "games": 3, "weekly_points": {"1": 20.0, "2": 18.0, "3": 22.0},
                        "season_total_points": 60.0, "season_average_points": 20.0,
                        "recent_average_points": 20.0,
                    },
                    {
                        # Better numbers than the real free agent above - if
                        # this shows up first, the status filter isn't working.
                        "gsis_id": RETIRED_GSIS, "name": "Long Retired", "team": "IND",
                        "games": 3, "weekly_points": {"1": 30.0, "2": 30.0, "3": 30.0},
                        "season_total_points": 90.0, "season_average_points": 30.0,
                        "recent_average_points": 30.0,
                    },
                ],
            }
        return {"season": 2025, "position": path.rsplit("/", 1)[-1], "scoring_not_applied": [], "players": []}

    monkeypatch.setattr(main.client, "league", fake_league)
    monkeypatch.setattr(main.client, "rosters", fake_rosters)
    monkeypatch.setattr(main.public, "post", fake_post)

    with TestClient(main.app) as c:
        yield c


def test_a_rostered_player_never_appears_as_available(client):
    response = client.get("/leagues/main/available?position=RB", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    ids = [p["player_id"] for p in body["available"]["RB"]]
    assert FREE_AGENT_ID in ids
    assert "4034" not in ids  # rostered on team 1 in fixtures.ROSTERS


def test_a_player_with_no_active_status_is_excluded_even_unrostered(client):
    """nflverse keeps an old stat line under a retired player's gsis_id, and
    nobody has rostered them either - but they are not a real waiver pickup,
    so no status at all (Sleeper's own signal for "not on an NFL roster")
    must keep them out regardless of how good their numbers look."""
    response = client.get("/leagues/main/available?position=RB", headers=HEADERS)
    ids = [p["player_id"] for p in response.json()["available"]["RB"]]
    assert RETIRED_ID not in ids


def test_unsupported_scoring_keys_are_reported_not_hidden(client):
    response = client.get("/leagues/main/available?position=RB", headers=HEADERS)
    assert "idp_tkl" in response.json()["scoring_not_applied"]


def test_an_unknown_position_is_rejected(client):
    response = client.get("/leagues/main/available?position=OL", headers=HEADERS)
    assert response.status_code == 400


def test_omitting_position_checks_all_four_skill_positions(client):
    response = client.get("/leagues/main/available", headers=HEADERS)
    assert response.status_code == 200
    assert set(response.json()["available"]) == {"QB", "RB", "WR", "TE"}


def test_requires_the_api_key(client):
    assert client.get("/leagues/main/available").status_code == 401


# --- schedule-difficulty -------------------------------------------------------


@pytest.fixture
def schedule_client(monkeypatch, tmp_path):
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

    async def fake_get(path, params=None):
        assert path == "/schedule/2025"
        return {
            "season": 2025,
            "opponents": {
                "KC": {"5": "SF", "6": "GB"},
                "SF": {"5": "KC", "6": "DAL"},
            },
        }

    async def fake_post(path, params=None, json=None):
        if path == "/points-allowed/QB":
            return {"teams": [{"team": "SF", "average_points_allowed": 14.0, "rank_stingiest": 3}]}
        if path == "/points-allowed/RB":
            return {"teams": [{"team": "SF", "average_points_allowed": 22.0, "rank_stingiest": 20}]}
        return {"teams": []}

    monkeypatch.setattr(main.client, "nfl_state", fake_state)
    monkeypatch.setattr(main.client, "league", fake_league)
    monkeypatch.setattr(main.client, "users", fake_users)
    monkeypatch.setattr(main.client, "rosters", fake_rosters)
    monkeypatch.setattr(main.public, "get", fake_get)
    monkeypatch.setattr(main.public, "post", fake_post)

    with TestClient(main.app) as c:
        yield c


def test_schedule_difficulty_maps_each_skill_player_to_their_opponents(schedule_client):
    response = schedule_client.get(
        "/leagues/main/schedule-difficulty/elchubi?weeks_ahead=2", headers=HEADERS
    )
    assert response.status_code == 200
    body = response.json()
    assert body["weeks_checked"] == [5, 6]

    by_name = {p["name"]: p for p in body["players"]}
    mahomes = by_name["Patrick Mahomes"]  # QB on KC, facing SF in week 5
    assert mahomes["weeks"][0]["opponent"] == "SF"
    assert mahomes["weeks"][0]["average_points_allowed"] == 14.0
    assert mahomes["weeks"][0]["rank_stingiest"] == 3
    # No schedule data at all for week 6's opponent (GB) at QB in the fixture.
    assert mahomes["weeks"][1]["opponent"] == "GB"
    assert mahomes["weeks"][1]["average_points_allowed"] is None


def test_schedule_difficulty_404s_on_an_unmatched_manager(schedule_client):
    response = schedule_client.get(
        "/leagues/main/schedule-difficulty/nobody-here", headers=HEADERS
    )
    assert response.status_code == 404
