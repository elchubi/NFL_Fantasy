"""Game-day weather from Open-Meteo (no API key required).

Only matters for kickers and the deep passing game in open-air stadiums, so
indoor venues short-circuit to `indoor: true` without an API call at all.
Forecasts are cached for 12h during the week and 1h on game day, when the
forecast actually moves.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from app.cache import KeyedDiskCache
from app.config import get_settings
from app.http import request_json
from app.teams import STADIUMS, normalise_abbr, stadium_for

log = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = 1

HOURLY_FIELDS = (
    "temperature_2m",
    "apparent_temperature",
    "precipitation_probability",
    "precipitation",
    "wind_speed_10m",
    "wind_gusts_10m",
    "snowfall",
)

# Wind is the variable that actually moves kicking and deep passing.
WIND_CONCERN_MPH = 15.0
WIND_SEVERE_MPH = 20.0


class WeatherProvider:
    def __init__(self, client: httpx.AsyncClient) -> None:
        settings = get_settings()
        self._client = client
        self._settings = settings
        self.cache = KeyedDiskCache(
            settings.cache_path("weather_cache.json"),
            name="weather",
            default_ttl_seconds=settings.weather_cache_ttl_hours * 3600,
            schema_version=CACHE_SCHEMA_VERSION,
        )

    def _ttl_for(self, kickoff: datetime | None) -> float:
        """Refresh hourly once kickoff is within a day, else twice a day."""
        settings = self._settings
        if kickoff is None:
            return settings.weather_cache_ttl_hours * 3600
        hours_out = (kickoff - datetime.now(timezone.utc)).total_seconds() / 3600
        if -6 <= hours_out <= 24:
            return settings.weather_gameday_cache_ttl_hours * 3600
        return settings.weather_cache_ttl_hours * 3600

    async def for_venue(
        self,
        home_team: str,
        kickoff: datetime | None = None,
    ) -> dict[str, Any]:
        """Forecast at one team's stadium, or an indoor flag for domes."""
        abbr = normalise_abbr(home_team)
        stadium = stadium_for(abbr)
        if stadium is None:
            return {
                "home_team": home_team,
                "known_venue": False,
                "weather": None,
                "note": f"No stadium on file for '{home_team}'.",
            }

        base = {
            "home_team": abbr,
            "known_venue": True,
            "stadium": stadium["stadium"],
            "roof": stadium["roof"],
            "indoor": stadium["indoor"],
        }
        if stadium["indoor"]:
            # No API call: a dome has no weather worth reporting.
            return {
                **base,
                "weather": None,
                "note": "Indoor stadium; weather does not affect this game.",
            }

        target = (kickoff or datetime.now(timezone.utc)).astimezone(timezone.utc)
        key = f"{abbr}:{target.date().isoformat()}:{target.hour:02d}"
        data, meta = await self.cache.get_or_refresh(
            key,
            lambda: self._fetch(stadium, target),
            ttl=self._ttl_for(kickoff),
        )
        return {**base, "kickoff": target.isoformat(), "weather": data, "cache": meta}

    async def _fetch(self, stadium: dict[str, Any], target: datetime) -> dict[str, Any]:
        start = target.date()
        payload = await request_json(
            self._client,
            self._settings.weather_base_url,
            source="Open-Meteo",
            params={
                "latitude": stadium["lat"],
                "longitude": stadium["lon"],
                "hourly": ",".join(HOURLY_FIELDS),
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
                "timezone": "UTC",
                "start_date": start.isoformat(),
                "end_date": (start + timedelta(days=1)).isoformat(),
            },
            timeout=self._settings.http_timeout,
            max_retries=self._settings.http_max_retries,
        )
        return summarise_forecast(payload, target)


def summarise_forecast(payload: Any, target: datetime) -> dict[str, Any]:
    """Pick the forecast hour closest to kickoff and add a fantasy read."""
    hourly = (payload or {}).get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return {"available": False, "note": "Open-Meteo returned no hourly forecast."}

    index = _closest_hour(times, target)
    if index is None:
        return {
            "available": False,
            "note": "Kickoff falls outside the returned forecast window.",
        }

    def at(field: str) -> Any:
        series = hourly.get(field) or []
        return series[index] if index < len(series) else None

    wind = at("wind_speed_10m")
    gusts = at("wind_gusts_10m")
    temperature = at("temperature_2m")
    precipitation_chance = at("precipitation_probability")
    snow = at("snowfall")

    return {
        "available": True,
        "forecast_time": times[index],
        "temperature_f": temperature,
        "feels_like_f": at("apparent_temperature"),
        "wind_mph": wind,
        "wind_gusts_mph": gusts,
        "precipitation_chance_pct": precipitation_chance,
        "precipitation_in": at("precipitation"),
        "snowfall_in": snow,
        "fantasy_impact": _impact(wind, gusts, temperature, precipitation_chance, snow),
    }


def _closest_hour(times: list[str], target: datetime) -> int | None:
    best_index, best_delta = None, None
    for index, stamp in enumerate(times):
        try:
            moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        delta = abs((moment - target).total_seconds())
        if best_delta is None or delta < best_delta:
            best_index, best_delta = index, delta
    # More than 12 hours away is not a forecast for this game.
    if best_delta is not None and best_delta > 12 * 3600:
        return None
    return best_index


def _impact(
    wind: float | None,
    gusts: float | None,
    temperature: float | None,
    precipitation_chance: float | None,
    snow: float | None,
) -> dict[str, Any]:
    notes: list[str] = []
    severity = "none"

    effective_wind = max(w for w in (wind or 0, gusts or 0)) if (wind or gusts) else 0
    if effective_wind >= WIND_SEVERE_MPH:
        severity = "high"
        notes.append(
            f"{effective_wind:.0f} mph wind: meaningful drag on field goals and deep passing"
        )
    elif effective_wind >= WIND_CONCERN_MPH:
        severity = "moderate"
        notes.append(f"{effective_wind:.0f} mph wind: some risk for kickers and deep shots")

    if snow:
        severity = "high"
        notes.append("snow in the forecast")
    elif precipitation_chance is not None and precipitation_chance >= 60:
        severity = "moderate" if severity == "none" else severity
        notes.append(f"{precipitation_chance:.0f}% chance of precipitation")

    if temperature is not None and temperature <= 25:
        severity = "moderate" if severity == "none" else severity
        notes.append(f"{temperature:.0f}F at kickoff")

    return {
        "severity": severity,
        "affects_kickers": severity != "none",
        "notes": notes or ["No weather concerns."],
    }


def all_stadiums() -> dict[str, Any]:
    return {abbr: dict(meta) for abbr, meta in STADIUMS.items()}
