"""Offline tests for the MCP layer."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BACKEND_URL", "http://backend:8000")
os.environ.setdefault("BACKEND_API_KEY", "test-backend-key")

import httpx  # noqa: E402
import pytest  # noqa: E402

from backend import Backend, BackendError, _error_message  # noqa: E402


def _response(status: int, payload=None, text: str | None = None) -> httpx.Response:
    request = httpx.Request("GET", "http://backend:8000/x")
    if text is not None:
        return httpx.Response(status, text=text, request=request)
    return httpx.Response(status, json=payload, request=request)


# --- Backend client -----------------------------------------------------------


async def test_the_api_key_is_attached_and_none_params_are_dropped():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    backend = Backend(base_url="http://backend:8000", api_key="k")
    backend._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://backend:8000"
    )

    result = await backend.get("/snapshot", params={"week": 5, "days": None, "include": None})
    assert result == {"ok": True}
    assert captured["headers"]["x-api-key"] == "k"
    # None params must not reach the backend as the string "None".
    assert captured["url"] == "http://backend:8000/snapshot?week=5"


async def test_health_is_called_without_the_api_key():
    """/health takes no key, and sending one would hide a misconfigured key
    behind an error from the one endpoint meant to diagnose it."""
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"status": "ok"})

    backend = Backend(base_url="http://backend:8000", api_key="k")
    backend._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://backend:8000"
    )

    await backend.get("/health", authenticated=False)
    assert "x-api-key" not in captured["headers"]


async def test_a_missing_api_key_fails_before_the_call_is_made():
    backend = Backend(base_url="http://backend:8000", api_key="")
    with pytest.raises(BackendError, match="BACKEND_API_KEY is not configured"):
        await backend.get("/snapshot")


async def test_network_failures_become_readable_errors():
    async def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    backend = Backend(base_url="http://backend:8000", api_key="k")
    backend._client = httpx.AsyncClient(
        transport=httpx.MockTransport(boom), base_url="http://backend:8000"
    )
    with pytest.raises(BackendError, match="Could not reach the league backend"):
        await backend.get("/snapshot")


# --- Error messages -----------------------------------------------------------


def test_the_backends_own_detail_is_preserved():
    """Those messages are written to be acted on, so they are what should reach
    the model rather than a bare status code."""
    message = _error_message(
        _response(404, {"detail": "Sleeper has no gsis_id for player 'KC'."}), "GET", "/x"
    )
    assert "no gsis_id for player 'KC'" in message
    assert "404" in message


def test_a_structured_detail_survives_as_json():
    detail = {"message": "matches more than one team", "candidates": [{"team_name": "A"}]}
    message = _error_message(_response(409, {"detail": detail}), "GET", "/roster/x")
    assert "matches more than one team" in message
    assert "candidates" in message
    assert json.loads(message.split(": ", 1)[1]) == detail


def test_a_401_points_at_the_key_rather_than_the_status():
    message = _error_message(_response(401, {"detail": "Missing or invalid"}), "GET", "/x")
    assert "BACKEND_API_KEY" in message


def test_a_body_with_no_detail_still_says_what_failed():
    message = _error_message(_response(500, {}), "GET", "/snapshot")
    assert "500" in message and "/snapshot" in message


def test_a_non_json_body_is_truncated_not_dropped():
    message = _error_message(_response(502, text="<html>Bad Gateway</html>"), "GET", "/x")
    assert "Bad Gateway" in message


# --- Server wiring ------------------------------------------------------------


def test_the_url_token_becomes_the_path(monkeypatch):
    import server

    monkeypatch.setenv("MCP_URL_TOKEN", "s3cret-token")
    assert server.mcp_path() == "/s3cret-token/mcp"
    # A token pasted with slashes should not produce a double slash.
    monkeypatch.setenv("MCP_URL_TOKEN", "/s3cret-token/")
    assert server.mcp_path() == "/s3cret-token/mcp"
    monkeypatch.delenv("MCP_URL_TOKEN")
    assert server.mcp_path() == "/mcp"


def test_allowed_hosts_gain_a_port_wildcard(monkeypatch):
    """The transport matches Host exactly - there is no `*` wildcard - and some
    proxies pass the port through, so both forms have to be listed."""
    import server

    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com, other.example.com")
    settings = server.transport_security()
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == [
        "mcp.example.com",
        "mcp.example.com:*",
        "other.example.com",
        "other.example.com:*",
    ]
    assert "https://mcp.example.com" in settings.allowed_origins


def test_protection_is_disabled_when_no_hosts_are_configured(monkeypatch):
    import server

    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    assert server.transport_security().enable_dns_rebinding_protection is False


def test_a_host_already_carrying_a_port_is_not_given_a_wildcard(monkeypatch):
    import server

    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "localhost:8080")
    assert server.transport_security().allowed_hosts == ["localhost:8080"]
