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

from app import __version__, enrichment, services
from app.config import get_settings
from app.espn import EspnProvider
from app.http import build_client
from app.nflverse import NflverseProvider, require_gsis
from app.odds import OddsProvider
from app.players import PlayerStore
from app.security import require_api_key
from app.sleeper import SleeperClient
from app.teams import STADIUMS, normalise_abbr
from app.weather import WeatherProvider, all_stadiums

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sleeper-api")

client = SleeperClient()
players = PlayerStore(client)

# One shared HTTP client for the external sources (nflverse, The Odds API,
# ESPN, Open-Meteo). Sleeper keeps its own inside SleeperClient.
external_http = build_client(settings.http_timeout)
nflverse = NflverseProvider(external_http)
odds = OddsProvider(external_http)
espn = EspnProvider(external_http)
weather = WeatherProvider(external_http)

PROVIDERS = {"nflverse": nflverse, "odds": odds, "espn": espn, "weather": weather}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.league_id:
        log.warning("LEAGUE_ID is not set; league endpoints will return 503.")
    if not settings.api_key:
        log.warning("API_KEY is not set; protected endpoints will return 503.")
    # Warm the in-memory copy from disk; the network refresh happens lazily on
    # the first request so a cold Sleeper never blocks startup.
    players.load_from_disk()
    for provider in PROVIDERS.values():
        provider.cache.load()
    if not settings.odds_api_key:
        log.info("ODDS_API_KEY is not set; /odds will return 503 until it is.")
    yield
    await client.aclose()
    await external_http.aclose()


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
        "sources": {
            "sleeper": True,
            "nflverse": True,
            "odds_api": bool(settings.odds_api_key),
            "espn": True,
            "open_meteo": True,
        },
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
    include: str | None = Query(
        default=None,
        description=(
            "Comma-separated external sources to attach: "
            "advanced_stats, odds, injury_report, weather. "
            "Omitted by default to keep the response small."
        ),
    ),
) -> dict[str, Any]:
    """Teams, rosters (starters/bench), standings, matchups and transactions.

    Pass `?include=` to attach any of the external sources; each one is fetched
    concurrently and a source that fails is reported inline rather than failing
    the whole snapshot.
    """
    return await services.build_snapshot(
        client,
        players,
        league_id(),
        week,
        days,
        includes=enrichment.parse_includes(include),
        providers=PROVIDERS,
    )


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


@app.get(
    "/advanced-stats/{player_id}",
    tags=["external"],
    dependencies=[Depends(require_api_key)],
    summary="nflverse usage and efficiency for one player",
)
async def advanced_stats(
    player_id: str = Path(
        description="Sleeper player id (the ids that appear in /snapshot rosters)."
    ),
    season: int | None = Query(
        default=None,
        ge=1999,
        le=2100,
        description="Season to pull. Defaults to the current NFL season.",
    ),
) -> dict[str, Any]:
    """Snap share, target share, air yards, red zone touches and EPA.

    Joined from the nflverse weekly releases on `gsis_id`, the id Sleeper
    carries on every player. Includes a season average, a last-three-week
    average and the delta between them - the earliest read on a role change.
    """
    await players.ensure_fresh()
    gsis = require_gsis(players, player_id)
    target_season = season or await current_season()
    result = await nflverse.for_gsis_id(gsis, target_season)
    sleeper_player = players.resolve(player_id)
    return {"player": sleeper_player, "gsis_id": gsis, **result}


@app.get(
    "/odds/{week}",
    tags=["external"],
    dependencies=[Depends(require_api_key)],
    summary="Betting lines for a week (game script signal)",
)
async def odds_for_week(
    week: int = Path(ge=1, le=22, description="NFL week."),
    season: int | None = Query(default=None, ge=1999, le=2100),
) -> dict[str, Any]:
    """Spread, total, moneyline and who is favoured, per game.

    Cached for a day to protect the free tier's ~500 requests/month; the quota
    The Odds API reports back is included in the response.
    """
    return await odds.for_week(week, season or await current_season())


@app.get(
    "/injury-report",
    tags=["external"],
    dependencies=[Depends(require_api_key)],
    summary="ESPN injury report for one NFL team",
)
async def injury_report_by_team(
    team: str = Query(
        description=f"NFL team abbreviation, one of: {', '.join(sorted(STADIUMS))}."
    ),
) -> dict[str, Any]:
    """Every listed injury for a team, with practice participation when ESPN
    includes it in the note (full / limited / did_not_practice)."""
    return await espn.team_report(team)


@app.get(
    "/injury-report/{player_id}",
    tags=["external"],
    dependencies=[Depends(require_api_key)],
    summary="ESPN injury detail for one player",
)
async def injury_report_by_player(
    player_id: str = Path(description="Sleeper player id."),
) -> dict[str, Any]:
    """ESPN's report for a single player, next to what Sleeper has cached.

    Matched on `espn_id` from the Sleeper player file, falling back to an exact
    name match within the player's own team.
    """
    await players.ensure_fresh()
    sleeper_player = players.resolve(player_id)
    if not sleeper_player["resolved"]:
        raise HTTPException(
            status_code=404, detail=f"'{player_id}' is not a known Sleeper player id."
        )

    team = normalise_abbr(sleeper_player.get("nfl_team"))
    if not team:
        raise HTTPException(
            status_code=404,
            detail=(
                f"{sleeper_player['name']} has no NFL team on file, so there is no "
                "ESPN team report to look them up in."
            ),
        )

    report = await espn.team_report(team)
    espn_id = players.espn_id(player_id)
    needle = " ".join(sleeper_player["name"].lower().split())

    match = None
    for item in report.get("injuries", []):
        if espn_id and str(item.get("espn_id")) == espn_id:
            match = item
            break
        if " ".join(str(item.get("name", "")).lower().split()) == needle:
            match = item

    return {
        "player": sleeper_player,
        "nfl_team": team,
        "espn_id": espn_id,
        "listed": match is not None,
        "espn_report": match,
        "sleeper_injury_status": sleeper_player.get("injury_status"),
        "note": (
            None
            if match
            else f"{sleeper_player['name']} is not on ESPN's injury report for {team}."
        ),
        "source_available": report.get("source_available"),
        "cache": report.get("cache"),
    }


@app.get(
    "/weather/{week}",
    tags=["external"],
    dependencies=[Depends(require_api_key)],
    summary="Kickoff weather for a week's outdoor venues",
)
async def weather_for_week(
    week: int = Path(ge=1, le=22, description="NFL week."),
    season: int | None = Query(default=None, ge=1999, le=2100),
) -> dict[str, Any]:
    """Forecast at each stadium, with domes flagged `indoor` and never fetched.

    The week's fixtures come from ESPN's scoreboard; the forecast itself comes
    from Open-Meteo at the home stadium's coordinates.
    """
    return await enrichment.weather_block(
        espn, weather, week, season or await current_season()
    )


@app.get(
    "/stadiums",
    tags=["external"],
    dependencies=[Depends(require_api_key)],
    summary="The static stadium reference used for weather",
)
async def stadiums() -> dict[str, Any]:
    """Coordinates and roof type per team - useful for checking the dome list."""
    return {"count": len(STADIUMS), "stadiums": all_stadiums()}


async def current_season() -> int:
    """The season Sleeper considers current, falling back to the calendar."""
    try:
        state = await client.nfl_state()
        return int(state.get("season"))
    except (HTTPException, TypeError, ValueError):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        return now.year if now.month >= 3 else now.year - 1


if __name__ == "__main__":  # pragma: no cover - local convenience
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
