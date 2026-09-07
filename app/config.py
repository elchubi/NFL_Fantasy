"""Runtime configuration, read from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Required for anything useful to happen.
    league_id: str = ""
    api_key: str = ""

    # Local player cache, hydrated from the public-data service rather than
    # from Sleeper directly (see app/public_client.py). Still kept on disk
    # here so roster resolution stays a synchronous, in-memory lookup.
    players_cache_path: str = "data/players_cache.json"
    players_cache_ttl_hours: float = 20.0

    # Where the per-source cache files live (defaults sit next to the player
    # cache so a single mounted volume covers all of them).
    cache_dir: str = ""

    # SQLite database holding the league's own history: transactions, draft
    # picks, roster snapshots and decisions. Empty means "league.db under the
    # cache directory". Betting lines and injury reports live in the shared
    # public-data service instead, since they are not league-specific.
    database_path: str = ""
    # Archive whatever a read endpoint pulls fresh from upstream, on top of the
    # scheduled /capture. Set false to archive only on /capture.
    history_auto_capture: bool = True

    # --- The shared public-data service ----------------------------------------
    # Player names, advanced stats, betting lines, injury reports, weather and
    # the draft board all live in a separate service, reached over Railway's
    # private network - see public_data/README.md. One instance is shared by
    # every league backend.
    public_data_url: str = "http://localhost:8100"
    public_data_api_key: str = ""
    public_data_timeout: float = 60.0

    # Sleeper API (league-specific calls: rosters, matchups, transactions,
    # drafts). /players/nfl and /state/nfl are NOT called from here any more -
    # those come from the public-data service.
    sleeper_base_url: str = "https://api.sleeper.app/v1"
    http_timeout: float = 20.0
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
