"""Minimal Sleeper client: only the two calls this service needs.

/players/nfl and /state/nfl are the only Sleeper endpoints that are not tied
to a specific league. Everything league-specific (rosters, transactions,
drafts) lives in each league backend's own client instead.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.http import build_client, request_json

log = logging.getLogger(__name__)


class SleeperClient:
    def __init__(self, client: Any | None = None) -> None:
        settings = get_settings()
        self._base_url = settings.sleeper_base_url.rstrip("/")
        self._max_retries = max(1, settings.http_max_retries)
        self._client = client or build_client(
            settings.http_timeout, user_agent="sleeper-fantasy-public-data/1.0"
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def nfl_state(self) -> dict[str, Any]:
        data = await request_json(
            self._client,
            f"{self._base_url}/state/nfl",
            source="Sleeper",
            max_retries=self._max_retries,
        )
        return data or {}

    async def all_players(self) -> dict[str, Any]:
        """The ~5MB NFL player file. Call at most once a day (see PlayerStore)."""
        from fastapi import HTTPException

        settings = get_settings()
        data = await request_json(
            self._client,
            f"{self._base_url}/players/nfl",
            source="Sleeper",
            timeout=settings.players_http_timeout,
            max_retries=self._max_retries,
        )
        if not isinstance(data, dict) or not data:
            raise HTTPException(
                status_code=502, detail="Sleeper returned an empty player file."
            )
        return data
