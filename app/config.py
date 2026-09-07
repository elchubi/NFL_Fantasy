"""Runtime configuration, read from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Required for anything useful to happen.
    league_id: str = ""
    api_key: str = ""

    # Player cache.
    players_cache_path: str = "data/players_cache.json"
    players_cache_ttl_hours: float = 20.0

    # --- External data sources ------------------------------------------------
    # Where the per-source cache files live (defaults sit next to the player
    # cache so a single mounted volume covers all of them).
    cache_dir: str = ""

    # nflverse (github releases, no key required).
    nflverse_base_url: str = "https://github.com/nflverse/nflverse-data/releases/download"
    nflverse_cache_ttl_hours: float = 24.0
    nflverse_download_timeout: float = 300.0
    # The play-by-play file is ~98MB; it is the only source of red zone usage.
    # Set to false to skip it and serve every other advanced stat.
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

    # SQLite database holding the league's own history: transactions, draft
    # picks, roster snapshots, betting lines, injury reports and decisions.
    # Empty means "league.db under the cache directory".
    database_path: str = ""
    # Archive whatever a read endpoint pulls fresh from upstream, on top of the
    # scheduled /capture. Set false to archive only on /capture.
    history_auto_capture: bool = True

    # Open-Meteo (no key).
    weather_base_url: str = "https://api.open-meteo.com/v1/forecast"
    weather_cache_ttl_hours: float = 12.0
    # Closer to kickoff the forecast is worth refreshing more often.
    weather_gameday_cache_ttl_hours: float = 1.0

    # HTTP.
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
        """Path for a source cache file, alongside the player cache by default."""
        from pathlib import Path

        base = Path(self.cache_dir) if self.cache_dir else Path(self.players_cache_path).parent
        return str(base / filename)

    def database_file(self) -> str:
        """Path to the SQLite database."""
        from pathlib import Path

        if self.database_path:
            return self.database_path
        base = Path(self.cache_dir) if self.cache_dir else Path(self.players_cache_path).parent
        return str(base / "league.db")


@lru_cache
def get_settings() -> Settings:
    return Settings()
