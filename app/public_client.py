"""HTTP client for the shared public-data service.

Player names, advanced stats, betting lines, injury reports, weather and the
draft board all live in a separate service now (see public_data/), reached
over Railway's private network so its Odds API quota, nflverse downloads and
Sleeper player-file fetches are paid once and shared by every league backend,
not once per league.

Errors are raised as `fastapi.HTTPException` with the same status code and
`detail` the public-data service used, so every endpoint here that used to
compute this data locally now proxies it with byte-for-byte identical
behaviour from the outside - and so `enrichment.py`'s existing `_guarded()`
helper, which already catches `HTTPException`, keeps working unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import HTTPException

log = logging.getLogger(__name__)


class PublicDataClient:
    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": "sleeper-fantasy-api/1.0 (public-data client)"},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params)

    async def post(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        return await self._request("POST", path, params, json)

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"X-API-Key": self.api_key} if self.api_key else {}

        try:
            response = await self._client.request(
                method, path, params=clean, json=json, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise HTTPException(
                status_code=504,
                detail=f"The public-data service timed out on {method} {path}.",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Could not reach the public-data service at {self.base_url}: {exc}",
            ) from exc

        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=_detail(response))

        try:
            return response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"The public-data service returned a non-JSON body for {method} {path}.",
            ) from exc

    # --- PlayerStore contract --------------------------------------------------
    # PlayerStore only ever calls `all_players()` on whatever client it is given
    # (see app/players.py), so this is the one method it needs to hydrate a
    # league backend's own local player cache from the shared service instead
    # of from Sleeper directly. The response is already in PlayerStore's own
    # trimmed format, and `_slim()` is idempotent on already-trimmed data, so
    # no other change to PlayerStore is needed.

    async def all_players(self) -> dict[str, Any]:
        payload = await self.get("/players")
        players = payload.get("players") if isinstance(payload, dict) else None
        if not players:
            raise HTTPException(
                status_code=502,
                detail="The public-data service returned an empty player map.",
            )
        return players


def _detail(response: httpx.Response) -> Any:
    """Pass through the public-data service's own `detail`, whatever shape it is."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:400] or f"{response.status_code} from the public-data service."
    if isinstance(body, dict) and "detail" in body:
        return body["detail"]
    return body
