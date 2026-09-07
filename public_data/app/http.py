"""Shared HTTP helpers for the external data sources.

Every upstream (Sleeper, nflverse, The Odds API, ESPN, Open-Meteo) is a free
public service, so each call gets a timeout, bounded retries with exponential
backoff on transient failures, and a clean HTTP error for the caller.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


def build_client(timeout: float, user_agent: str = "sleeper-fantasy-api/1.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        headers={"User-Agent": f"{user_agent} (+read-only)"},
        follow_redirects=True,
    )


async def request_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    source: str,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
    max_retries: int = 3,
    allow_404: bool = False,
) -> Any:
    """GET `url` and decode JSON, retrying transient failures."""
    last_error: Exception | None = None

    for attempt in range(1, max(1, max_retries) + 1):
        try:
            response = await client.get(url, params=params, timeout=timeout)
        except httpx.TimeoutException as exc:
            last_error = exc
            log.warning("[%s] timeout calling %s (attempt %s)", source, url, attempt)
        except httpx.HTTPError as exc:
            last_error = exc
            log.warning("[%s] network error calling %s (attempt %s): %s", source, url, attempt, exc)
        else:
            if response.status_code == 404:
                if allow_404:
                    return None
                raise HTTPException(status_code=404, detail=f"{source} has no data at {url}.")
            if response.status_code in (401, 403):
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"{source} rejected the request ({response.status_code}). "
                        "Check the API key for that source."
                    ),
                )
            if response.status_code in RETRY_STATUS:
                last_error = httpx.HTTPStatusError(
                    f"{response.status_code} from {source}",
                    request=response.request,
                    response=response,
                )
                log.warning(
                    "[%s] returned %s for %s (attempt %s)",
                    source,
                    response.status_code,
                    url,
                    attempt,
                )
            elif response.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"{source} returned {response.status_code} for {url}.",
                )
            else:
                try:
                    return response.json()
                except ValueError as exc:
                    raise HTTPException(
                        status_code=502,
                        detail=f"{source} returned a non-JSON body for {url}.",
                    ) from exc

        if attempt < max_retries:
            await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

    raise HTTPException(
        status_code=504,
        detail=f"Could not reach {source} at {url}: {last_error}",
    )


async def download_to_file(
    client: httpx.AsyncClient,
    url: str,
    destination: Path,
    *,
    source: str,
    timeout: float,
    allow_404: bool = False,
) -> bool:
    """Stream a (potentially large) file to disk. False if it 404s and allow_404."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    try:
        async with client.stream("GET", url, timeout=timeout) as response:
            if response.status_code == 404 and allow_404:
                log.info("[%s] no file at %s", source, url)
                return False
            if response.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"{source} returned {response.status_code} for {url}.",
                )
            with tmp.open("wb") as fh:
                async for chunk in response.aiter_bytes(chunk_size=1 << 16):
                    fh.write(chunk)
    except httpx.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status_code=504, detail=f"Could not download {url} from {source}: {exc}"
        ) from exc

    tmp.replace(destination)
    return True
