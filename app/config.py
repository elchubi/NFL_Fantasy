"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import re
from functools import lru_cache

from fastapi import HTTPException
from pydantic_settings import BaseSettings, SettingsConfigDict

# A slug becomes both a URL path segment and a filename ("league-<slug>.db"),
# so it is restricted to what is safe in both.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Required for anything useful to happen.
    #
    # One backend can serve several leagues: `LEAGUES` is a comma-separated
    # list of `slug:sleeper_league_id` pairs, e.g.
    #   LEAGUES=main:1390746710426255360,dynasty:9876543210
    # Each slug gets its own database file and its own URL prefix
    # (/leagues/{slug}/...). A single-league setup is just one pair.
    #
    # All leagues on one backend share one API_KEY - this backend is meant for
    # one person's own leagues, not multiple separate parties, so per-league
    # keys would add real complexity (routing a key to a league, rotating one
    # without affecting the others) for no isolation benefit anyone here needs.
    leagues: str = ""
    api_key: str = ""

    # Local player cache, hydrated from the public-data service rather than
    # from Sleeper directly (see app/public_client.py). Still kept on disk
    # here so roster resolution stays a synchronous, in-memory lookup. Shared
    # by every league on this backend, since player data is not per-league.
    players_cache_path: str = "data/players_cache.json"
    players_cache_ttl_hours: float = 20.0

    # Where the per-source cache files live (defaults sit next to the player
    # cache so a single mounted volume covers all of them).
    cache_dir: str = ""

    # Archive whatever a read endpoint pulls fresh from upstream, on top of the
    # scheduled /capture. Set false to archive only on /capture.
    history_auto_capture: bool = True

    # --- The shared public-data service ----------------------------------------
    # Player names, advanced stats, betting lines, injury reports, weather and
    # the draft board all live in a separate service, reached over Railway's
    # private network - see public_data/README.md. One instance is shared by
    # every league backend, and every league on this backend.
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

    @property
    def league_map(self) -> dict[str, str]:
        """slug -> Sleeper league_id, parsed from LEAGUES."""
        pairs: dict[str, str] = {}
        for entry in self.leagues.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" not in entry:
                raise ValueError(
                    f"LEAGUES entry '{entry}' is missing a slug - expected "
                    "'slug:sleeper_league_id', e.g. 'main:1390746710426255360'."
                )
            slug, _, league_id = entry.partition(":")
            slug, league_id = slug.strip(), league_id.strip()
            if not _SLUG_RE.match(slug):
                raise ValueError(
                    f"LEAGUES slug '{slug}' must be lowercase letters, digits, "
                    "'-' or '_', starting with a letter or digit, 32 chars max."
                )
            if not league_id:
                raise ValueError(f"LEAGUES entry for slug '{slug}' has no league id.")
            pairs[slug] = league_id
        return pairs

    def league_id_for(self, slug: str) -> str:
        """The Sleeper league_id for a configured slug, or a 404."""
        league_id = self.league_map.get(slug)
        if league_id is None:
            known = ", ".join(sorted(self.league_map)) or "none configured"
            raise HTTPException(
                status_code=404,
                detail=f"No league '{slug}' is configured on this backend. Known: {known}.",
            )
        return league_id

    def cache_path(self, filename: str) -> str:
        """Path for a source cache file, alongside the player cache by default."""
        from pathlib import Path

        base = Path(self.cache_dir) if self.cache_dir else Path(self.players_cache_path).parent
        return str(base / filename)

    def database_file(self, slug: str) -> str:
        """Path to one league's SQLite database."""
        return self.cache_path(f"league-{slug}.db")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.league_map  # noqa: B018 - validate LEAGUES eagerly, fail at startup not mid-request
    return settings
