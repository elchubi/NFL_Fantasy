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


@lru_cache
def get_settings() -> Settings:
    return Settings()
