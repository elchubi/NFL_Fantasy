"""Betting lines from The Odds API (https://the-odds-api.com).

Spread and total are the cleanest public proxy for game script: big favourites
run the ball late, big underdogs throw. The free tier allows roughly 500
requests a month, so responses are cached for a full day by default and the
remaining quota that the API reports back is surfaced on every response.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import HTTPException

from app.cache import KeyedDiskCache
from app.config import get_settings
from app.teams import nfl_team_abbr

log = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = 1
SPORT_KEY = "americanfootball_nfl"


class OddsProvider:
    def __init__(self, client: httpx.AsyncClient) -> None:
        settings = get_settings()
        self._client = client
        self._settings = settings
        self.cache = KeyedDiskCache(
            settings.cache_path("odds_cache.json"),
            name="odds",
            default_ttl_seconds=settings.odds_cache_ttl_hours * 3600,
            schema_version=CACHE_SCHEMA_VERSION,
        )

    @property
    def configured(self) -> bool:
        return bool(self._settings.odds_api_key)

    async def for_week(self, week: int, season: int | None = None) -> dict[str, Any]:
        if not self.configured:
            raise HTTPException(
                status_code=503,
                detail=(
                    "ODDS_API_KEY is not configured. Get a free key at "
                    "https://the-odds-api.com and set ODDS_API_KEY."
                ),
            )
        key = f"week:{season or 'current'}:{week}"
        data, meta = await self.cache.get_or_refresh(key, self._fetch)
        games = [g for g in data.get("games", []) if g.get("week") in (None, week)]
        return {
            "week": week,
            "games": games or data.get("games", []),
            "quota": data.get("quota"),
            "cache": meta,
            "note": (
                "The Odds API returns upcoming games rather than NFL week numbers; "
                "games are matched to a week by kickoff date."
            ),
        }

    async def _fetch(self) -> dict[str, Any]:
        settings = self._settings
        params: dict[str, Any] = {
            "apiKey": settings.odds_api_key,
            "regions": settings.odds_regions,
            "markets": "spreads,totals,h2h",
            "oddsFormat": "american",
            "dateFormat": "iso",
        }
        if settings.odds_bookmakers:
            params["bookmakers"] = settings.odds_bookmakers

        url = f"{settings.odds_base_url.rstrip('/')}/sports/{SPORT_KEY}/odds"
        # The quota headers only come back on the raw response, so this call is
        # made directly rather than through request_json.
        try:
            response = await self._client.get(url, params=params, timeout=settings.http_timeout)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=504, detail=f"Could not reach The Odds API: {exc}"
            ) from exc

        if response.status_code == 401:
            raise HTTPException(
                status_code=502, detail="The Odds API rejected ODDS_API_KEY (401)."
            )
        if response.status_code == 429:
            raise HTTPException(
                status_code=502,
                detail="The Odds API monthly quota is exhausted (429).",
            )
        if response.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"The Odds API returned {response.status_code}.",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=502, detail="The Odds API returned a non-JSON body."
            ) from exc

        return {
            "games": [parse_game(game) for game in payload if isinstance(game, dict)],
            "quota": {
                "requests_remaining": response.headers.get("x-requests-remaining"),
                "requests_used": response.headers.get("x-requests-used"),
                "last_request_cost": response.headers.get("x-requests-last"),
            },
        }


def parse_game(game: dict[str, Any]) -> dict[str, Any]:
    """Flatten one Odds API event into home/away, spread, total and favourite.

    The consensus line is the median across the returned bookmakers, so one
    outlier book cannot skew it.
    """
    home = game.get("home_team")
    away = game.get("away_team")
    kickoff = game.get("commence_time")

    spreads: dict[str, list[float]] = {}
    totals: list[float] = []
    moneylines: dict[str, list[float]] = {}

    for book in game.get("bookmakers") or []:
        for market in book.get("markets") or []:
            key = market.get("key")
            for outcome in market.get("outcomes") or []:
                name = outcome.get("name")
                if key == "spreads" and outcome.get("point") is not None:
                    spreads.setdefault(name, []).append(float(outcome["point"]))
                elif key == "totals" and outcome.get("point") is not None:
                    if str(outcome.get("name", "")).lower() == "over":
                        totals.append(float(outcome["point"]))
                elif key == "h2h" and outcome.get("price") is not None:
                    moneylines.setdefault(name, []).append(float(outcome["price"]))

    home_spread = _median(spreads.get(home, []))
    away_spread = _median(spreads.get(away, []))
    total = _median(totals)

    favourite = None
    spread_magnitude = None
    if home_spread is not None and away_spread is not None:
        if home_spread < away_spread:
            favourite, spread_magnitude = home, abs(home_spread)
        elif away_spread < home_spread:
            favourite, spread_magnitude = away, abs(away_spread)
    elif home_spread is not None:
        favourite = home if home_spread < 0 else away
        spread_magnitude = abs(home_spread)

    implied = None
    if total is not None and spread_magnitude is not None:
        # Standard decomposition: each side's implied points from total+spread.
        implied = {
            "favourite": round(total / 2 + spread_magnitude / 2, 2),
            "underdog": round(total / 2 - spread_magnitude / 2, 2),
        }

    return {
        "game_id": game.get("id"),
        "kickoff": kickoff,
        "week": _week_from_kickoff(kickoff),
        "home_team": home,
        "home_team_abbr": nfl_team_abbr(home),
        "away_team": away,
        "away_team_abbr": nfl_team_abbr(away),
        "home_spread": home_spread,
        "away_spread": away_spread,
        "total": total,
        "favourite": favourite,
        "favourite_abbr": nfl_team_abbr(favourite),
        "spread": spread_magnitude,
        "moneyline": {
            "home": _median(moneylines.get(home, [])),
            "away": _median(moneylines.get(away, [])),
        },
        "implied_team_totals": implied,
        "bookmakers_counted": len(game.get("bookmakers") or []),
        "game_script": _game_script_note(favourite, spread_magnitude, total),
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 2)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 2)


def _game_script_note(favourite: str | None, spread: float | None, total: float | None) -> str | None:
    if favourite is None or spread is None:
        return None
    parts = []
    if spread >= 7:
        parts.append(
            f"{favourite} favoured by {spread}: leans run-heavy late for the favourite "
            "and pass volume for the underdog"
        )
    elif spread <= 3:
        parts.append(f"Close game ({spread}-point spread): neutral game script")
    else:
        parts.append(f"{favourite} favoured by {spread}: mild positive script")
    if total is not None:
        if total >= 48:
            parts.append(f"high total ({total}) points to volume for both offences")
        elif total <= 40:
            parts.append(f"low total ({total}) caps scoring upside")
    return "; ".join(parts)


def _week_from_kickoff(kickoff: str | None) -> int | None:
    """Best-effort NFL week from a kickoff timestamp.

    The Odds API has no week field. Weeks are anchored to the Thursday of NFL
    week 1, which lands in the first full week of September.
    """
    if not kickoff:
        return None
    try:
        moment = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    season = moment.year if moment.month >= 3 else moment.year - 1
    # The NFL opens on the Thursday after Labor Day (the first Monday of
    # September), so week 1 runs from that Thursday.
    september = datetime(season, 9, 1, tzinfo=timezone.utc)
    labor_day = september + timedelta(days=(7 - september.weekday()) % 7)
    week_one_thursday = labor_day + timedelta(days=3)
    delta_days = (moment - week_one_thursday).days
    if delta_days < 0:
        return None
    return min(22, delta_days // 7 + 1)
