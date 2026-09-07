"""Simple shared-secret auth via the X-API-Key header."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from app.config import get_settings


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Reject the request unless X-API-Key matches the configured API_KEY.

    Fails closed: if API_KEY is not configured the endpoint is unusable rather
    than open, since the service is meant to be exposed publicly.
    """
    expected = get_settings().api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_KEY is not configured on the server; refusing to serve data.",
        )
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key header.",
        )
