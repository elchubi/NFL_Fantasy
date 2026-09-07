"""Runtime configuration for the shared public-data service.

Nothing here is league-specific: no LEAGUE_ID, no per-league API_KEY. This
service deploys once and every league backend points at it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Shared secret required in the X-API-Key header on every endpoint except
    # /health. Independent from any league backend's own API_KEY.
    api_key: str = ""

    # Where cache files and the odds/injury archive live.
    cache_dir: str = "data"
    database_path: str = ""

    # Sleeper player file.
    players_cache_path: str = "data/players_cache.json"
    players_cache_ttl_hours: float = 20.0

    # nflverse (github releases, no key required).
    nflverse_base_url: str = "https://github.com/nflverse/nflverse-data/releases/download"
    nflverse_cache_ttl_hours: float = 24.0
    nflverse_download_timeout: float = 300.0
    nflverse_include_red_zone: bool = True

    # The Odds API (free tier: ~500 requests/month, so cache hard).
    odds_api_key: str = ""
    odds_base_url: str = "https://api.the-odds-api.com/v4"
    odds_cache_ttl_hours: float = 24.0
    odds_regions: str = "us"
    odds_bookmakers: str = ""

    # ESPN (unofficial, no key). Injury news moves during the week.
    espn_base_url: str = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
    espn_cache_ttl_hours: float = 3.0

    # Open-Meteo (no key).
    weather_base_url: str = "https://api.open-meteo.com/v1/forecast"
    weather_cache_ttl_hours: float = 12.0
    weather_gameday_cache_ttl_hours: float = 1.0

    # Also archive whatever a read endpoint pulls fresh from upstream, on top
    # of the scheduled /capture. Set false to archive only on /capture.
    history_auto_capture: bool = True

    # Sleeper API (only /players/nfl and /state/nfl are used here).
    sleeper_base_url: str = "https://api.sleeper.app/v1"
    http_timeout: float = 20.0
    players_http_timeout: float = 120.0
    http_max_retries: int = 3

    cors_origins: str = "*"
    log_level: str = "INFO"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def cache_path(self, filename: str) -> str:
        return str(Path(self.cache_dir) / filename)


    def database_file(self) -> str:
        return self.database_path or self.cache_path("public.db")


@lru_cache
def get_settings() -> Settings:
    return Settings()
