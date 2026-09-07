"""Protocol-level tests: what a client actually sees over Streamable HTTP."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BACKEND_URL", "http://backend:8000")
os.environ.setdefault("BACKEND_API_KEY", "test-backend-key")

import httpx  # noqa: E402
import pytest  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import server  # noqa: E402

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


def _decode(response: httpx.Response):
    """Streamable HTTP answers as SSE; pull the JSON out of the last event."""
    events = [line[5:] for line in response.text.splitlines() if line.startswith("data:")]
    return json.loads(events[-1]) if events else json.loads(response.text)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("MCP_URL_TOKEN", "test-token")
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    return server.mcp.streamable_http_app(
        streamable_http_path=server.mcp_path(),
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


def _call(client, payload):
    return _decode(client.post("/test-token/mcp", json=payload, headers=HEADERS))


def test_the_server_is_served_at_the_token_path(app):
    with TestClient(app) as client:
        result = _call(client, INITIALIZE)["result"]
        assert result["serverInfo"]["name"] == "sleeper-fantasy-league"
        # The un-prefixed path must not answer.
        assert client.post("/mcp", json=INITIALIZE, headers=HEADERS).status_code == 404


def test_every_tool_is_listed_with_a_description(app):
    with TestClient(app) as client:
        _call(client, INITIALIZE)
        tools = _call(
            client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )["result"]["tools"]

    names = {t["name"] for t in tools}
    assert names == set(server.TOOL_NAMES)
    assert len(names) == 28
    for tool in tools:
        # The description is what the model reads to decide whether to call it,
        # so an empty or stub one is a real defect.
        assert len(tool.get("description") or "") > 60, tool["name"]
        assert tool["inputSchema"]["type"] == "object"


def test_annotations_mark_what_moves_state(app):
    with TestClient(app) as client:
        _call(client, INITIALIZE)
        tools = _call(
            client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )["result"]["tools"]
    by_name = {t["name"]: t.get("annotations") or {} for t in tools}

    writers = {
        "decision_log", "decision_log_outcome", "history_capture", "history_backfill",
    }
    for name, annotations in by_name.items():
        if name in writers:
            assert annotations.get("readOnlyHint") is False, name
            # Every write here only ever appends.
            assert annotations.get("destructiveHint") is False, name
        else:
            assert annotations.get("readOnlyHint") is True, name

    # Capture skips rows identical to the last recorded state and backfill skips
    # finished seasons, so repeating either is safe; the decision log records
    # every call on purpose and is not idempotent.
    assert by_name["history_capture"].get("idempotentHint") is True
    assert by_name["history_backfill"].get("idempotentHint") is True
    assert by_name["decision_log"].get("idempotentHint") is not True


def test_tool_schemas_carry_the_documented_defaults(app):
    with TestClient(app) as client:
        _call(client, INITIALIZE)
        tools = _call(
            client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )["result"]["tools"]
    schemas = {t["name"]: t["inputSchema"] for t in tools}

    assert schemas["league_snapshot"]["properties"]["days"]["default"] == 7
    # manager_list's `days` now defaults to null rather than 180: omitting it
    # means "use the whole archive", which is what makes the profile
    # multi-season instead of one season deep.
    assert schemas["manager_list"]["properties"]["days"]["default"] is None
    assert schemas["manager_list"]["properties"]["seasons"]["default"] is None
    assert schemas["history_backfill"]["properties"]["limit"]["default"] == 20
    assert schemas["manager_pressure"]["properties"]["horizon"]["default"] == 3
    assert schemas["history_capture"]["properties"]["refresh"]["default"] is True
    assert schemas["draft_class"]["properties"]["landing"]["default"] is True
    # Required arguments must not be optional in the schema.
    assert "season" in schemas["draft_class"]["required"]
    assert "manager" in schemas["league_roster"]["required"]


def test_enum_arguments_are_constrained(app):
    with TestClient(app) as client:
        _call(client, INITIALIZE)
        tools = _call(
            client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )["result"]["tools"]
    schemas = {t["name"]: t["inputSchema"] for t in tools}

    source = schemas["history_source"]["properties"]["source"]
    assert set(source["enum"]) == {"odds", "injuries", "decisions"}
    kind = schemas["decision_log"]["properties"]["kind"]
    assert "waiver_bid" in kind["enum"] and "start_sit" in kind["enum"]


def test_a_backend_error_reaches_the_model_with_its_message(app, monkeypatch):
    """A bare exception is reported as "Error executing tool <name>" and the
    backend's actual message is lost, which is the message worth having."""

    async def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"detail": "Sleeper has no gsis_id for player 'KC'."}, request=request
        )

    server.backend._client = httpx.AsyncClient(
        transport=httpx.MockTransport(failing), base_url="http://backend:8000"
    )
    with TestClient(app) as client:
        _call(client, INITIALIZE)
        result = _call(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "stats_advanced", "arguments": {"player_id": "KC"}},
            },
        )["result"]

    assert result["isError"] is True
    assert "no gsis_id for player 'KC'" in result["content"][0]["text"]


def test_a_successful_call_returns_the_backend_payload(app):
    async def ok(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-backend-key"
        assert request.url.path == "/leagues/main/snapshot"
        return httpx.Response(200, json={"week": 7, "teams": []}, request=request)

    server.backend._client = httpx.AsyncClient(
        transport=httpx.MockTransport(ok), base_url="http://backend:8000"
    )
    with TestClient(app) as client:
        _call(client, INITIALIZE)
        result = _call(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "league_snapshot", "arguments": {"league": "main", "week": 7}},
            },
        )["result"]

    assert result.get("isError") is not True
    assert json.loads(result["content"][0]["text"])["week"] == 7


def test_a_league_tool_without_league_or_default_asks_to_pick_one(app):
    with TestClient(app) as client:
        _call(client, INITIALIZE)
        result = _call(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "league_snapshot", "arguments": {}},
            },
        )["result"]

    assert result["isError"] is True
    assert "league_list" in result["content"][0]["text"]


def test_default_league_is_used_when_the_call_omits_one(app, monkeypatch):
    monkeypatch.setenv("DEFAULT_LEAGUE", "main")

    async def ok(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/leagues/main/league-settings"
        return httpx.Response(200, json={"format": {}}, request=request)

    server.backend._client = httpx.AsyncClient(
        transport=httpx.MockTransport(ok), base_url="http://backend:8000"
    )
    with TestClient(app) as client:
        _call(client, INITIALIZE)
        result = _call(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "league_settings", "arguments": {}},
            },
        )["result"]

    assert result.get("isError") is not True


def test_the_health_route_is_outside_the_token_path(app):
    """The platform probes this without knowing the secret segment."""
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["tools"] == 28
        assert body["backend_api_key_configured"] is True


def test_an_unlisted_host_is_rejected_when_protection_is_on(monkeypatch):
    monkeypatch.setenv("MCP_URL_TOKEN", "test-token")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com")

    def guarded_app():
        # The session manager can only be started once per app instance, so
        # each client gets its own.
        return server.mcp.streamable_http_app(
            streamable_http_path=server.mcp_path(),
            stateless_http=True,
            transport_security=server.transport_security(),
        )

    with TestClient(guarded_app(), base_url="https://evil.example") as client:
        response = client.post(
            "/test-token/mcp",
            json=INITIALIZE,
            headers={**HEADERS, "Host": "evil.example"},
        )
        assert response.status_code == 421

    with TestClient(guarded_app(), base_url="https://mcp.example.com") as client:
        response = client.post(
            "/test-token/mcp",
            json=INITIALIZE,
            headers={**HEADERS, "Host": "mcp.example.com"},
        )
        assert response.status_code == 200
