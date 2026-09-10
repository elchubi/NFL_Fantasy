"""Integration tests for GET /leagues/{league}/players/compare.

Exercises real HTTP routing through main.app with the Sleeper client and the
public-data client monkeypatched, since the actual upstreams are unreachable
in this environment - same pattern as test_available.py, which this endpoint
deliberately reuses the production logic of (minus the free-agent filter).
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

# fixtures.PLAYERS_RAW: "4034" Christian McCaffrey (RB), "6794" Justin
# Jefferson (WR), "1466" Travis Kelce (TE), "5849" Kyler Murray (QB),
# "K1" Justin Tucker (K) - "Justin" alone matches two players by prefix.
MCCAFFREY_GSIS = "00-0034796"
JEFFERSON_GSIS = "00-0036322"
KELCE_GSIS = "00-0087556"
# Kyler Murray gets a gsis_id but no row in the fake QB response, to exercise
# "resolved to a skill position, but nothing to report" separately from
# "not a skill position at all" (Tucker, K) and "not a known id" (garbage).
MURRAY_GSIS = "00-0035228"


@pytest.fixture
def client(monkeypatch, tmp_path):
    import main

    monkeypatch.setattr(main.players, "_path", tmp_path / "no-such-cache.json")
    main.players._players = {
        pid: _slim(pid, raw) for pid, raw in fixtures.PLAYERS_RAW.items()
    }
    main.players._players["4034"]["gsis_id"] = MCCAFFREY_GSIS
    main.players._players["6794"]["gsis_id"] = JEFFERSON_GSIS
    main.players._players["1466"]["gsis_id"] = KELCE_GSIS
    main.players._players["5849"]["gsis_id"] = MURRAY_GSIS
    main.players._reindex()
    main.players._fetched_at = time.time()

    async def fake_league(league_id):
        return fixtures.LEAGUE

    async def fake_post(path, params=None, json=None):
        assert json is not None and "scoring_settings" in json
        if path == "/position-points/RB":
            return {
                "season": 2025, "position": "RB", "scoring_not_applied": [],
                "players": [
                    {
                        "gsis_id": MCCAFFREY_GSIS, "name": "Christian McCaffrey", "team": "SF",
                        "games": 3, "season_total_points": 60.0,
                        "season_average_points": 20.0, "recent_average_points": 20.0,
                    },
                ],
            }
        if path == "/position-points/WR":
            return {
                "season": 2025, "position": "WR", "scoring_not_applied": ["idp_tkl"],
                "players": [
                    {
                        "gsis_id": JEFFERSON_GSIS, "name": "Justin Jefferson", "team": "MIN",
                        "games": 3, "season_total_points": 45.0,
                        "season_average_points": 15.0, "recent_average_points": 15.0,
                    },
                ],
            }
        if path == "/position-points/TE":
            return {
                "season": 2025, "position": "TE", "scoring_not_applied": [],
                "players": [
                    {
                        "gsis_id": KELCE_GSIS, "name": "Travis Kelce", "team": "KC",
                        "games": 3, "season_total_points": 30.0,
                        "season_average_points": 10.0, "recent_average_points": 10.0,
                    },
                ],
            }
        if path == "/position-points/QB":
            # Murray's gsis_id is deliberately absent - injured all season,
            # say - to exercise "resolved but no production" separately.
            return {"season": 2025, "position": "QB", "scoring_not_applied": [], "players": []}
        raise AssertionError(f"unexpected position-points call: {path}")

    monkeypatch.setattr(main.client, "league", fake_league)
    monkeypatch.setattr(main.public, "post", fake_post)

    with TestClient(main.app) as c:
        yield c


def test_requires_at_least_one_player(client):
    response = client.get("/leagues/main/players/compare", headers=HEADERS)
    assert response.status_code == 400


def test_more_than_the_max_is_rejected(client):
    ids = ",".join(str(n) for n in range(21))
    response = client.get(f"/leagues/main/players/compare?ids={ids}", headers=HEADERS)
    assert response.status_code == 400


def test_resolves_by_id_with_the_same_shape_available_uses(client):
    response = client.get("/leagues/main/players/compare?ids=4034,6794", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["season"] == 2025
    assert body["unresolved"] == []
    by_name = {p["name"]: p for p in body["players"]}
    assert by_name["Christian McCaffrey"]["season_average_points"] == 20.0
    assert by_name["Christian McCaffrey"]["position"] == "RB"
    assert by_name["Justin Jefferson"]["recent_average_points"] == 15.0
    assert "idp_tkl" in body["scoring_not_applied"]


def test_a_rostered_player_is_included_unlike_available(client):
    # No roster lookup happens at all here - the point of this endpoint is
    # comparing players regardless of who has them, e.g. for a trade.
    response = client.get("/leagues/main/players/compare?ids=4034", headers=HEADERS)
    assert response.status_code == 200
    assert len(response.json()["players"]) == 1


def test_mixed_positions_are_fetched_and_merged_in_one_call(client):
    response = client.get(
        "/leagues/main/players/compare?ids=4034,6794,1466", headers=HEADERS
    )
    assert response.status_code == 200
    names = {p["name"] for p in response.json()["players"]}
    assert names == {"Christian McCaffrey", "Justin Jefferson", "Travis Kelce"}


def test_an_exact_name_match_resolves(client):
    response = client.get(
        "/leagues/main/players/compare?names=Justin Jefferson", headers=HEADERS
    )
    body = response.json()
    assert body["unresolved"] == []
    assert body["players"][0]["name"] == "Justin Jefferson"


def test_a_substring_only_name_match_resolves(client):
    # "efferson" is neither an exact nor a prefix match, only a substring one.
    response = client.get("/leagues/main/players/compare?names=efferson", headers=HEADERS)
    body = response.json()
    assert body["unresolved"] == []
    assert body["players"][0]["name"] == "Justin Jefferson"


def test_an_ambiguous_name_is_reported_not_guessed(client):
    # "justin" prefix-matches both Justin Jefferson and Justin Tucker.
    response = client.get("/leagues/main/players/compare?names=justin", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["players"] == []
    assert len(body["unresolved"]) == 1
    entry = body["unresolved"][0]
    assert "more than one player" in entry["reason"]
    assert {c["name"] for c in entry["candidates"]} == {"Justin Jefferson", "Justin Tucker"}


def test_an_unknown_id_is_reported_alongside_valid_ones(client):
    response = client.get(
        "/leagues/main/players/compare?ids=4034,not-a-real-id", headers=HEADERS
    )
    assert response.status_code == 200
    body = response.json()
    assert body["players"][0]["name"] == "Christian McCaffrey"
    assert len(body["unresolved"]) == 1
    assert "not-a-real-id" in body["unresolved"][0]["reason"]


def test_a_non_skill_position_is_reported_not_silently_dropped(client):
    response = client.get("/leagues/main/players/compare?ids=K1", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["players"] == []
    assert "only QB, RB, WR, TE have production data" in body["unresolved"][0]["reason"]


def test_a_skill_player_with_no_stat_row_yet_is_zero_production_not_unresolved(client):
    # Confirmed live: a player whose game this week just hasn't kicked off
    # yet (or a bye, or an early-season injury) has no nflverse row at all -
    # that is real, reportable information about them, not a failure to
    # resolve them, so it belongs in `players` with games: 0, not
    # `unresolved`.
    response = client.get("/leagues/main/players/compare?ids=5849", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["unresolved"] == []
    assert len(body["players"]) == 1
    murray = body["players"][0]
    assert murray["name"] == "Kyler Murray"
    assert murray["position"] == "QB"
    # Same "no sample" convention public_data's own scoring.py uses: a zero
    # total (sum of nothing), but None averages (there is no games to divide
    # by) - not zero, which would misleadingly read as "scored zero points".
    assert murray["games"] == 0
    assert murray["season_total_points"] == 0.0
    assert murray["season_average_points"] is None
    assert murray["recent_average_points"] is None
    # nfl_team still comes through from the player resolver even with no
    # production row to pull it from.
    assert murray["nfl_team"] == "ARI"


def test_a_mix_of_played_and_not_yet_played_players_both_come_back(client):
    # McCaffrey has a real stat row (see the client fixture); Murray doesn't.
    # Both belong in `players`, distinguished only by their numbers.
    response = client.get("/leagues/main/players/compare?ids=4034,5849", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["unresolved"] == []
    by_name = {p["name"]: p for p in body["players"]}
    assert by_name["Christian McCaffrey"]["games"] == 3
    assert by_name["Kyler Murray"]["games"] == 0


def test_requires_the_api_key(client):
    assert client.get("/leagues/main/players/compare?ids=4034").status_code == 401
