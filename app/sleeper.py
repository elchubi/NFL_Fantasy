"""Thin async client for the public Sleeper API.

Sleeper is rate limited to roughly 1000 requests/minute and needs no auth.
Every call gets a timeout, a couple of retries on transient failures, and is
translated into a clean HTTP error for the caller.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import HTTPException

from app.config import get_settings

log = logging.getLogger(__name__)

_RETRY_STATUS = {429, 500, 502, 503, 504}


class SleeperClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._base_url = settings.sleeper_base_url.rstrip("/")
        self._max_retries = max(1, settings.http_max_retries)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout),
            headers={"User-Agent": "sleeper-fantasy-api/1.0 (+read-only)"},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        path: str,
        *,
        timeout: float | None = None,
        allow_404: bool = False,
    ) -> Any:
        """GET a Sleeper path (e.g. "/league/123"). Returns decoded JSON.

        Sleeper answers with `null` for a lot of "nothing here" cases (e.g. a
        week with no matchups), which we normalise to None; callers decide
        whether that means an empty list or a real error.
        """
        url = f"{self._base_url}/{path.lstrip('/')}"
        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._client.get(url, timeout=timeout)
            except httpx.TimeoutException as exc:
                last_error = exc
                log.warning("Timeout calling %s (attempt %s)", url, attempt)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("Network error calling %s (attempt %s): %s", url, attempt, exc)
            else:
                if response.status_code == 404:
                    if allow_404:
                        return None
                    raise HTTPException(
                        status_code=404,
                        detail=f"Sleeper has no data for {path}.",
                    )
                if response.status_code in _RETRY_STATUS:
                    last_error = httpx.HTTPStatusError(
                        f"{response.status_code} from Sleeper",
                        request=response.request,
                        response=response,
                    )
                    log.warning(
                        "Sleeper returned %s for %s (attempt %s)",
                        response.status_code,
                        url,
                        attempt,
                    )
                else:
                    if response.status_code >= 400:
                        raise HTTPException(
                            status_code=502,
                            detail=(
                                f"Sleeper returned {response.status_code} for {path}."
                            ),
                        )
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise HTTPException(
                            status_code=502,
                            detail=f"Sleeper returned a non-JSON body for {path}.",
                        ) from exc

            if attempt < self._max_retries:
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

        raise HTTPException(
            status_code=504,
            detail=f"Could not reach the Sleeper API for {path}: {last_error}",
        )

    # --- Convenience wrappers -------------------------------------------------

    async def nfl_state(self) -> dict[str, Any]:
        return await self.get("/state/nfl") or {}

    async def league(self, league_id: str) -> dict[str, Any]:
        data = await self.get(f"/league/{league_id}")
        if not data:
            raise HTTPException(status_code=404, detail=f"League {league_id} not found.")
        return data

    async def users(self, league_id: str) -> list[dict[str, Any]]:
        return await self.get(f"/league/{league_id}/users") or []

    async def rosters(self, league_id: str) -> list[dict[str, Any]]:
        return await self.get(f"/league/{league_id}/rosters") or []

    async def matchups(self, league_id: str, week: int) -> list[dict[str, Any]]:
        data = await self.get(f"/league/{league_id}/matchups/{week}", allow_404=True)
        return data or []

    async def transactions(self, league_id: str, week: int) -> list[dict[str, Any]]:
        data = await self.get(f"/league/{league_id}/transactions/{week}", allow_404=True)
        return data or []

    async def drafts(self, league_id: str) -> list[dict[str, Any]]:
        """Drafts for a league. Keeper leagues have one per season."""
        data = await self.get(f"/league/{league_id}/drafts", allow_404=True)
        return data or []

    async def draft_picks(self, draft_id: str) -> list[dict[str, Any]]:
        data = await self.get(f"/draft/{draft_id}/picks", allow_404=True)
        return data or []

    async def all_players(self) -> dict[str, Any]:
        """The ~5MB NFL player file. Call at most once a day (see PlayerStore)."""
        settings = get_settings()
        data = await self.get("/players/nfl", timeout=settings.players_http_timeout)
        if not isinstance(data, dict) or not data:
            raise HTTPException(
                status_code=502,
                detail="Sleeper returned an empty player file.",
            )
        return data
