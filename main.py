"""FastAPI service that exposes a Sleeper fantasy league in readable form.

Every endpoint except /health requires the X-API-Key header. Everything is
read-only: data is pulled live from Sleeper on each request, with only the
~5MB NFL player file cached on disk.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware

from app import __version__, services
from app.config import get_settings
from app.players import PlayerStore
from app.security import require_api_key
from app.sleeper import SleeperClient

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sleeper-api")

client = SleeperClient()
players = PlayerStore(client)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.league_id:
        log.warning("LEAGUE_ID is not set; league endpoints will return 503.")
    if not settings.api_key:
        log.warning("API_KEY is not set; protected endpoints will return 503.")
    # Warm the in-memory copy from disk; the network refresh happens lazily on
    # the first request so a cold Sleeper never blocks startup.
    players.load_from_disk()
    yield
    await client.aclose()


app = FastAPI(
    title="Sleeper Fantasy League API",
    description=(
        "Read-only view of a Sleeper fantasy football league with player IDs "
        "already resolved to names, positions, NFL teams and injury status."
    ),
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def league_id() -> str:
    if not settings.league_id:
        raise HTTPException(
            status_code=503,
            detail="LEAGUE_ID is not configured on the server.",
        )
    return settings.league_id


@app.get("/health", tags=["ops"], summary="Healthcheck for Coolify")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "league_id_configured": bool(settings.league_id),
        "api_key_configured": bool(settings.api_key),
        "players_cache": players.status(),
    }


@app.get(
    "/snapshot",
    tags=["league"],
    dependencies=[Depends(require_api_key)],
    summary="Whole league state, fully resolved",
)
async def snapshot(
    week: int | None = Query(
        default=None,
        ge=1,
        le=22,
        description="NFL week for matchups. Defaults to the current week.",
    ),
    days: int = Query(
        default=7,
        ge=1,
        le=120,
        description="How many days of transactions to include.",
    ),
) -> dict[str, Any]:
    """Teams, rosters (starters/bench), standings, matchups and transactions."""
    return await services.build_snapshot(client, players, league_id(), week, days)


@app.get(
    "/league-settings",
    tags=["league"],
    dependencies=[Depends(require_api_key)],
    summary="League configuration in plain language",
)
async def league_settings() -> dict[str, Any]:
    """Scoring, roster slots, playoff format, trade deadline and waiver rules."""
    league = await client.league(league_id())
    return services.league_settings_view(league)


@app.get(
    "/roster/{manager}",
    tags=["league"],
    dependencies=[Depends(require_api_key)],
    summary="One resolved roster, found by username or team name",
)
async def roster(
    manager: str = Path(
        description="Username, display name or team name (case-insensitive, partial ok)."
    ),
) -> dict[str, Any]:
    """A single team's roster without pulling the whole league."""
    await players.ensure_fresh()
    lid = league_id()
    league, users, rosters = await asyncio.gather(
        client.league(lid), client.users(lid), client.rosters(lid)
    )

    teams = services.build_teams(users, rosters)
    match, candidates = services.find_team(teams, manager)

    if match is None:
        if candidates:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"'{manager}' matches more than one team.",
                    "candidates": [
                        {
                            "display_name": c["display_name"],
                            "team_name": c["team_name"],
                            "roster_id": c["roster_id"],
                        }
                        for c in candidates
                    ],
                },
            )
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"No team matches '{manager}'.",
                "available": [
                    {
                        "display_name": t["display_name"],
                        "team_name": t["team_name"],
                        "roster_id": t["roster_id"],
                    }
                    for t in teams.values()
                ],
            },
        )

    raw_roster = next(
        (r for r in rosters if r.get("roster_id") == match["roster_id"]), {}
    )
    resolved = services.resolve_roster(
        raw_roster, match, league.get("roster_positions") or [], players
    )
    return {
        "league_id": league.get("league_id"),
        "season": league.get("season"),
        "matched_on": manager,
        "team": resolved,
        "players_cache": players.status(),
    }


if __name__ == "__main__":  # pragma: no cover - local convenience
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
