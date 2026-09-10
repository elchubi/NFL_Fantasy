"""HTTP-level tests for the public-data service's own endpoints."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from tests import fixtures_external as fx  # noqa: E402

PLAYERS_RAW = {
    "4034": {
        "first_name": "Christian", "last_name": "McCaffrey", "position": "RB",
        "team": "SF", "status": "Active", "gsis_id": "00-0034796", "espn_id": 3117251,
    },
    "6794": {
        "first_name": "Justin", "last_name": "Jefferson", "position": "WR",
        "team": "MIN", "status": "Active", "gsis_id": "00-0036322",
    },
}


async def fake_sleeper_get(self, url, params=None, headers=None, timeout=None):
    class Resp:
        status_code = 200

        def json(self_inner):
            if "players/nfl" in url:
                return PLAYERS_RAW
            if "state/nfl" in url:
                return {"week": 2, "display_week": 2, "season": "2025"}
            raise AssertionError(url)

    return Resp()


def test_health_needs_no_api_key(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_sleeper_get)
    with TestClient(main.app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


def test_players_endpoint_serves_the_trimmed_map(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_sleeper_get)
    with TestClient(main.app) as client:
        h = {"X-API-Key": "test-key"}
        response = client.get("/players", headers=h)
        assert response.status_code == 200
        body = response.json()
        assert "4034" in body["players"]
        # /players serves the internal trimmed format (full_name, not the
        # resolve()-shaped "name") - it is meant to be re-ingested by a
        # league backend's own PlayerStore, which is idempotent on this shape.
        assert body["players"]["4034"]["full_name"] == "Christian McCaffrey"
        assert body["players"]["4034"]["gsis_id"] == "00-0034796"


def test_players_endpoint_requires_the_api_key():
    with TestClient(main.app) as client:
        assert client.get("/players").status_code == 401


def test_advanced_stats_404s_for_a_player_with_no_gsis_id(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_sleeper_get)
    with TestClient(main.app) as client:
        h = {"X-API-Key": "test-key"}
        response = client.get("/advanced-stats/does-not-exist", headers=h)
        assert response.status_code == 404


def test_byes_endpoint_requires_the_api_key():
    with TestClient(main.app) as client:
        assert client.get("/byes/2026").status_code == 401
