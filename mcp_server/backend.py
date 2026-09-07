"""HTTP client for the league backend.

The MCP server never holds league data of its own; every tool is a thin,
typed wrapper over one REST endpoint. `BACKEND_API_KEY` lives here and is
attached to each internal call, so the Claude client never sees it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)


class BackendError(Exception):
    """A backend call failed in a way worth showing the model verbatim."""


class Backend:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("BACKEND_URL", "http://localhost:8000")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("BACKEND_API_KEY", "")
        self.timeout = timeout or float(os.getenv("BACKEND_TIMEOUT", "60"))
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
                headers={"User-Agent": "sleeper-fantasy-mcp/1.0"},
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        """Call the backend and return decoded JSON.

        Errors are raised as BackendError with the backend's own message, since
        those messages are written to be actionable ("no gsis_id for this
        player", "matches more than one team, here are the candidates") and are
        more useful to the model than a bare status code.
        """
        if authenticated and not self.api_key:
            raise BackendError(
                "BACKEND_API_KEY is not configured on the MCP server, so it cannot "
                "authenticate against the league backend."
            )

        headers = {"X-API-Key": self.api_key} if authenticated else {}
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        client = await self.client()

        try:
            response = await client.request(method, path, params=clean, headers=headers)
        except httpx.TimeoutException as exc:
            raise BackendError(f"The league backend timed out on {method} {path}.") from exc
        except httpx.HTTPError as exc:
            raise BackendError(
                f"Could not reach the league backend at {self.base_url}: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise BackendError(_error_message(response, method, path))

        try:
            return response.json()
        except ValueError as exc:
            raise BackendError(
                f"The league backend returned a non-JSON body for {method} {path}."
            ) from exc

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self.request("POST", path, **kwargs)


def _error_message(response: httpx.Response, method: str, path: str) -> str:
    """Surface the backend's own `detail`, which is written to be actionable."""
    detail: Any = None
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("detail")
    except ValueError:
        detail = response.text[:400] or None

    if response.status_code == 401:
        return (
            "The league backend rejected the MCP server's API key (401). "
            "Check BACKEND_API_KEY."
        )
    if response.status_code == 503 and not detail:
        return "The league backend is not fully configured (503)."

    if detail is None:
        return f"The league backend returned {response.status_code} for {method} {path}."
    if isinstance(detail, (dict, list)):
        import json

        return f"{response.status_code} from the league backend: {json.dumps(detail)}"
    return f"{response.status_code} from the league backend: {detail}"
