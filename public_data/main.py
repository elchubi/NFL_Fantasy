"""Shared public NFL data - deployed once, reached over Railway's private
network by every league backend.

Nothing here depends on a specific fantasy league: player names, advanced
usage stats, betting lines, injury reports, stadium weather and the rookie
draft board are the same regardless of who is asking. Splitting these out of
the league backend means the expensive or quota-limited parts - nflverse's
~120MB downloads, The Odds API's ~500/month free tier, ESPN's fetch cadence -
are paid once, not once per league.

Every endpoint except /health requires the X-API-Key header. This service has
no LEAGUE_ID and no concept of managers, rosters or transactions - that stays
in each league backend, which is the only thing that calls this one.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import get_settings
from app.db import Database
from app.draft import DraftProvider, landing_spot, require_prospect
from app.espn import EspnProvider
from app.history import SOURCES, auto_capture, capture_week, injury_rows, odds_rows
from app.http import build_client
from app.nflverse import NflverseProvider, require_gsis
from app.odds import OddsProvider
from app.players import PlayerStore
from app.schedule import ScheduleProvider
from app.security import require_api_key
from app import store
from app.sleeper import SleeperClient
from app.teams import STADIUMS, normalise_abbr
from app.weather import WeatherProvider, all_stadiums

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("public-data")

sleeper = SleeperClient()
players = PlayerStore(sleeper)

http_client = build_client(settings.http_timeout, user_agent="sleeper-fantasy-public-data/1.0")
nflverse = NflverseProvider(http_client)
odds = OddsProvider(http_client)
espn = EspnProvider(http_client)
weather = WeatherProvider(http_client)
draft = DraftProvider(http_client)
schedule = ScheduleProvider(http_client)

CACHED_PROVIDERS = {
    "nflverse": nflverse, "odds": odds, "espn": espn,
    "weather": weather, "draft": draft, "schedule": schedule,
}

db = Database(settings.database_file())


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.api_key:
        log.warning("API_KEY is not set; every endpoint but /health will return 503.")
    players.load_from_disk()
    for provider in CACHED_PROVIDERS.values():
        provider.cache.load()
    log.info("Database ready: %s", db.stats())
    if not settings.odds_api_key:
        log.info("ODDS_API_KEY is not set; /odds will return 503 until it is.")
    yield
    await sleeper.aclose()
    await http_client.aclose()
    db.close()


app = FastAPI(
    title="Sleeper Fantasy Public Data",
    description=(
        "Shared NFL data reused by every league backend: player names, advanced "
        "stats, betting lines, injury reports, stadium weather and the rookie "
        "draft board. Not specific to any fantasy league."
    ),
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health", tags=["ops"], summary="Healthcheck for the platform")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "api_key_configured": bool(settings.api_key),
        "odds_api_key_configured": bool(settings.odds_api_key),
        "players_cache": players.status(),
        "database": db.stats(),
    }


@app.get(
    "/players",
    dependencies=[Depends(require_api_key)],
    summary="The full trimmed Sleeper player map",
)
async def all_players() -> dict[str, Any]:
    """Every player Sleeper knows about, trimmed to the fields this service
    serves. League backends call this to hydrate their own local copy for
    roster resolution, instead of each one hitting Sleeper's player file
    directly - Sleeper asks integrators not to pull it more than once a day."""
    await players.ensure_fresh()
    return {"fetched_at": players.fetched_at, "players": players._players}


@app.get(
    "/advanced-stats/{player_id}",
    tags=["external"],
    dependencies=[Depends(require_api_key)],
    summary="nflverse usage and efficiency for one player",
)
async def advanced_stats(
    player_id: str = Path(description="Sleeper player id."),
    season: int | None = Query(default=None, ge=1999, le=2100),
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
    week: int = Path(ge=1, le=22),
    season: int | None = Query(default=None, ge=1999, le=2100),
) -> dict[str, Any]:
    """Spread, total, moneyline and who is favoured, per game.

    Cached for a day to protect the free tier's ~500 requests/month; the quota
    The Odds API reports back is included in the response.
    """
    target_season = season or await current_season()
    payload = await odds.for_week(week, target_season)
    if settings.history_auto_capture and (payload.get("cache") or {}).get("refreshed"):
        await auto_capture(db, "odds", target_season, week, odds_rows(payload["games"]))
    return payload


@app.get(
    "/injury-report",
    tags=["external"],
    dependencies=[Depends(require_api_key)],
    summary="ESPN injury report for one NFL team",
)
async def injury_report_by_team(
    team: str = Query(description=f"NFL team abbreviation, one of: {', '.join(sorted(STADIUMS))}.")
) -> dict[str, Any]:
    """Every listed injury for a team, with practice participation when ESPN
    includes it in the note (full / limited / did_not_practice)."""
    report = await espn.team_report(team)
    await _archive_injuries(report)
    return report


@app.get(
    "/injury-report/{player_id}",
    tags=["external"],
    dependencies=[Depends(require_api_key)],
    summary="ESPN injury detail for one player",
)
async def injury_report_by_player(player_id: str = Path(description="Sleeper player id.")) -> dict[str, Any]:
    """ESPN's report for a single player, next to what Sleeper has cached.

    Matched on `espn_id` from the Sleeper player file, falling back to an exact
    name match within the player's own team.
    """
    await players.ensure_fresh()
    sleeper_player = players.resolve(player_id)
    if not sleeper_player["resolved"]:
        raise HTTPException(status_code=404, detail=f"'{player_id}' is not a known Sleeper player id.")

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
    await _archive_injuries(report)
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
        "note": None if match else f"{sleeper_player['name']} is not on ESPN's injury report for {team}.",
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
    week: int = Path(ge=1, le=22),
    season: int | None = Query(default=None, ge=1999, le=2100),
) -> dict[str, Any]:
    """Forecast at each stadium, with domes flagged `indoor` and never fetched.

    The week's fixtures come from ESPN's scoreboard; the forecast itself comes
    from Open-Meteo at the home stadium's coordinates.
    """
    target_season = season or await current_season()
    scoreboard = await espn.schedule(week, target_season)
    games = scoreboard.get("games") or []
    if not games:
        return {
            "available": False,
            "error": "No schedule available from ESPN for this week, so there are no venues to fetch weather for.",
        }

    forecasts = await asyncio.gather(
        *[
            weather.for_venue(game["home_team"], _kickoff(game.get("kickoff")))
            for game in games
            if game.get("home_team")
        ],
        return_exceptions=True,
    )

    results = []
    for game, forecast in zip([g for g in games if g.get("home_team")], forecasts):
        if isinstance(forecast, BaseException):
            results.append(
                {"game": game.get("name"), "home_team": game.get("home_team"), "error": str(getattr(forecast, "detail", forecast))}
            )
            continue
        results.append({"game": game.get("name"), "away_team": game.get("away_team"), **forecast})

    return {
        "available": True,
        "week": week,
        "games": results,
        "outdoor_games_with_concerns": [
            g for g in results
            if (g.get("weather") or {}).get("fantasy_impact", {}).get("severity") in ("moderate", "high")
        ],
    }


def _kickoff(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@app.get(
    "/byes/{season}",
    tags=["external"],
    dependencies=[Depends(require_api_key)],
    summary="Bye week per NFL team for a season",
)
async def byes_for_season(season: int = Path(ge=1999, le=2100)) -> dict[str, Any]:
    """team abbreviation -> its bye week, derived from the nflverse schedule.

    Used by a league backend's /pressure endpoint to spot colliding byes -
    not tied to any specific league, so it lives here.
    """
    data, meta = await schedule.season(season)
    return {"season": season, "byes": data.get("byes") or {}, "cache": meta}


@app.get(
    "/stadiums",
    tags=["external"],
    dependencies=[Depends(require_api_key)],
    summary="The static stadium reference used for weather",
)
async def stadiums() -> dict[str, Any]:
    """Coordinates and roof type per team - useful for checking the dome list."""
    return {"count": len(STADIUMS), "stadiums": all_stadiums()}


@app.get(
    "/draft-class/{season}",
    tags=["draft"],
    dependencies=[Depends(require_api_key)],
    summary="Rookie draft board for one class",
)
async def draft_class(
    season: int = Path(ge=1980, le=2100, description="Draft year, e.g. 2026."),
    position: str | None = Query(default=None, description="Filter to one of QB, RB, WR, TE."),
    round_max: int | None = Query(default=None, ge=1, le=7),
    landing: bool = Query(default=True),
) -> dict[str, Any]:
    """Every skill-position pick with draft capital, age, combine and landing spot.

    Built from nflverse's draft_picks and combine releases. draft_picks carries
    `gsis_id`, so each prospect lines up with any league's Sleeper rosters.
    """
    data, meta = await draft.class_for(season)
    prospects = list((data.get("prospects") or {}).values())

    if position:
        wanted = position.strip().upper()
        prospects = [p for p in prospects if p["position"] == wanted]
    if round_max:
        prospects = [p for p in prospects if (p["draft"]["round"] or 99) <= round_max]

    prospects.sort(key=lambda p: (p["draft"]["round"] or 99, p["draft"]["pick_in_round"] or 999))

    if landing and prospects:
        await players.ensure_fresh()
        prior, _ = await nflverse.season_data(season - 1)
        prospects = [{**p, "landing_spot": landing_spot(p, prior, players)} for p in prospects]

    return {
        "season": season,
        "count": len(prospects),
        "counts": data.get("counts"),
        "prospects": [_with_sleeper(p) for p in prospects],
        "cache": meta,
        "note": (
            "Draft capital and age are the strongest rookie-season predictors; "
            "landing_spot is computed from last season's snap counts and each "
            "incumbent's current Sleeper team. No college data source is used."
        ),
    }


@app.get(
    "/prospect/{player_id}",
    tags=["draft"],
    dependencies=[Depends(require_api_key)],
    summary="Draft profile for one player",
)
async def prospect(
    player_id: str = Path(description="Sleeper player id, or an nflverse gsis_id."),
    season: int | None = Query(default=None, ge=1980, le=2100),
) -> dict[str, Any]:
    """One prospect's draft capital, combine numbers and landing spot."""
    await players.ensure_fresh()

    gsis = player_id if player_id.startswith("00-0") else players.gsis_id(player_id)
    if not gsis:
        raise HTTPException(
            status_code=404,
            detail=f"'{player_id}' has no gsis_id in the Sleeper player file, so it cannot be matched to a draft pick.",
        )

    target_season = season or await _draft_season_for(gsis)
    data, meta = await draft.class_for(target_season)
    found = require_prospect(data, gsis)

    prior, _ = await nflverse.season_data(target_season - 1)
    return {
        "season": target_season,
        "prospect": _with_sleeper({**found, "landing_spot": landing_spot(found, prior, players)}),
        "cache": meta,
    }


async def _draft_season_for(gsis: str) -> int:
    current = await current_season()
    for candidate in range(current, current - 6, -1):
        data, _ = await draft.class_for(candidate)
        if gsis in (data.get("prospects") or {}):
            return candidate
    raise HTTPException(
        status_code=404,
        detail=f"No skill-position pick in the last six draft classes matches {gsis}. Pass ?season= to check an older class.",
    )


def _with_sleeper(prospect: dict[str, Any]) -> dict[str, Any]:
    gsis = prospect.get("gsis_id")
    sleeper_id = players.sleeper_id_for_gsis(gsis) if gsis else None
    return {
        **prospect,
        "sleeper_player_id": sleeper_id,
        "sleeper_player": players.resolve(sleeper_id) if sleeper_id else None,
    }


@app.post(
    "/capture",
    tags=["history"],
    dependencies=[Depends(require_api_key)],
    summary="Archive this week's betting lines and injury reports",
)
async def capture(
    week: int | None = Query(default=None),
    season: int | None = Query(default=None),
    teams: str | None = Query(default=None, description="Comma-separated team abbreviations. Defaults to all 32."),
    refresh: bool = Query(default=True),
) -> dict[str, Any]:
    """Write this week's odds and injury reports to the append-only archive.

    Meant to be called from a scheduler, typically once on Thursday and once
    shortly before Sunday kickoff. Rows identical to the last recorded state
    are skipped.
    """
    target_season = season or await current_season()
    target_week = week or await current_week_number()

    if teams:
        requested = [normalise_abbr(t) for t in teams.split(",") if t.strip()]
        if any(t is None for t in requested):
            raise HTTPException(status_code=400, detail=f"Unknown team abbreviation(s) in '{teams}'.")
        team_list = requested
    else:
        team_list = sorted(STADIUMS)

    return await capture_week(
        db, odds_provider=odds, espn_provider=espn,
        season=target_season, week=target_week, teams=team_list, refresh=refresh,
    )


@app.get(
    "/history",
    tags=["history"],
    dependencies=[Depends(require_api_key)],
    summary="What is in the archive",
)
async def history_inventory() -> dict[str, Any]:
    return {
        "database": db.stats(),
        "archived": await store.inventory(db),
        "note": "Odds and injury reports only - the two sources that cannot be re-fetched later.",
    }


@app.get(
    "/history/{source}",
    tags=["history"],
    dependencies=[Depends(require_api_key)],
    summary="Read archived rows for one source",
)
async def history_rows(
    source: str = Path(description=f"One of: {', '.join(SOURCES)}."),
    season: int | None = Query(default=None, ge=1999, le=2100),
    week: int | None = Query(default=None, ge=1, le=22),
    limit: int | None = Query(default=None, ge=1, le=10000),
) -> dict[str, Any]:
    if source not in SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown history source '{source}'. Valid: {', '.join(SOURCES)}.")
    target_season = season or await current_season()
    if source == "odds":
        rows = await store.read_odds(db, target_season, week=week, limit=limit)
    else:
        rows = await store.read_injuries(db, target_season, week=week, limit=limit)
    return {"source": source, "season": target_season, "week": week, "count": len(rows), "rows": rows}


async def _archive_injuries(report: dict[str, Any]) -> None:
    if not settings.history_auto_capture:
        return
    if not (report.get("cache") or {}).get("refreshed"):
        return
    team = report.get("team")
    injuries = report.get("injuries") or []
    if not team or not injuries:
        return
    season, week = await _current_season_week()
    await auto_capture(db, "injuries", season, week, injury_rows(team, injuries))


_SEASON_WEEK_MEMO: dict[str, Any] = {"value": None, "at": 0.0}
_SEASON_WEEK_TTL = 3600.0


async def _current_season_week() -> tuple[int | None, int | None]:
    import time as _time

    if _SEASON_WEEK_MEMO["value"] and (_time.time() - _SEASON_WEEK_MEMO["at"]) < _SEASON_WEEK_TTL:
        return _SEASON_WEEK_MEMO["value"]
    try:
        state = await sleeper.nfl_state()
        value = (int(state.get("season")), _week_from_state(state))
    except (HTTPException, TypeError, ValueError) as exc:
        log.warning("Could not resolve the current season/week for auto-capture: %s", exc)
        return (None, None)
    _SEASON_WEEK_MEMO.update(value=value, at=_time.time())
    return value


def _week_from_state(state: dict[str, Any]) -> int:
    for key in ("display_week", "week", "leg"):
        value = state.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 1


async def current_week_number() -> int:
    state = await sleeper.nfl_state()
    return _week_from_state(state)


async def current_season() -> int:
    try:
        state = await sleeper.nfl_state()
        return int(state.get("season"))
    except (HTTPException, TypeError, ValueError):
        now = datetime.now(timezone.utc)
        return now.year if now.month >= 3 else now.year - 1


if __name__ == "__main__":  # pragma: no cover - local convenience
    import os
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
