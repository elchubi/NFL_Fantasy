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
from app.draft import DraftProvider, landing_spot, require_prospect
from app.espn import EspnProvider
from app.history import SOURCES as HISTORY_SOURCES
from app.history import HistoryStore, auto_capture, capture_week, injury_rows, odds_rows
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
draft = DraftProvider(external_http)

# Append-only archive for betting lines and injury reports - the only two
# sources that cannot be re-fetched from upstream once the week has passed.
history = HistoryStore(settings.history_path())

# Sources that keep a disk cache (warmed at startup).
CACHED_PROVIDERS = {
    "nflverse": nflverse,
    "odds": odds,
    "espn": espn,
    "weather": weather,
    "draft": draft,
}

# What the /snapshot blocks get. The history store rides along so those blocks
# can archive whatever they pull fresh, the same as the read endpoints do.
PROVIDERS = {
    **CACHED_PROVIDERS,
    "history": history if settings.history_auto_capture else None,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.league_id:
        log.warning("LEAGUE_ID is not set; league endpoints will return 503.")
    if not settings.api_key:
        log.warning("API_KEY is not set; protected endpoints will return 503.")
    # Warm the in-memory copy from disk; the network refresh happens lazily on
    # the first request so a cold Sleeper never blocks startup.
    players.load_from_disk()
    for provider in CACHED_PROVIDERS.values():
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
    target_season = season or await current_season()
    payload = await odds.for_week(week, target_season)
    if settings.history_auto_capture and (payload.get("cache") or {}).get("refreshed"):
        # Fresh from upstream, so this is a line state worth keeping.
        await auto_capture(history, "odds", target_season, week, odds_rows(payload["games"]))
    return payload


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
    report = await espn.team_report(team)
    await _archive_injuries(report)
    return report


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


@app.get(
    "/draft-class/{season}",
    tags=["draft"],
    dependencies=[Depends(require_api_key)],
    summary="Rookie draft board for one class",
)
async def draft_class(
    season: int = Path(ge=1980, le=2100, description="Draft year, e.g. 2026."),
    position: str | None = Query(
        default=None, description="Filter to one of QB, RB, WR, TE."
    ),
    round_max: int | None = Query(
        default=None, ge=1, le=7, description="Only picks in this round or earlier."
    ),
    landing: bool = Query(
        default=True,
        description=(
            "Compute how much work vacated at each player's position on his new "
            "team, from last season's snap counts."
        ),
    ),
) -> dict[str, Any]:
    """Every skill-position pick with draft capital, age, combine and landing spot.

    Built from nflverse's draft_picks and combine releases. draft_picks carries
    `gsis_id`, so each prospect lines up with the Sleeper rosters in /snapshot.
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
        prospects = [
            {**p, "landing_spot": landing_spot(p, prior, players)} for p in prospects
        ]

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
    season: int | None = Query(
        default=None, ge=1980, le=2100, description="Draft year. Defaults to a lookup."
    ),
) -> dict[str, Any]:
    """One prospect's draft capital, combine numbers and landing spot."""
    await players.ensure_fresh()

    # Accept either id: gsis ids look like 00-00xxxxx.
    gsis = player_id if player_id.startswith("00-0") else players.gsis_id(player_id)
    if not gsis:
        raise HTTPException(
            status_code=404,
            detail=(
                f"'{player_id}' has no gsis_id in the Sleeper player file, so it "
                "cannot be matched to a draft pick."
            ),
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
    """Find which class a gsis_id belongs to, newest first."""
    current = await current_season()
    for candidate in range(current, current - 6, -1):
        data, _ = await draft.class_for(candidate)
        if gsis in (data.get("prospects") or {}):
            return candidate
    raise HTTPException(
        status_code=404,
        detail=(
            f"No skill-position pick in the last six draft classes matches {gsis}. "
            "Pass ?season= to check an older class."
        ),
    )


def _with_sleeper(prospect: dict[str, Any]) -> dict[str, Any]:
    """Attach the Sleeper player id so the prospect lines up with /snapshot."""
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
    week: int | None = Query(
        default=None, ge=1, le=22, description="Week to capture. Defaults to the current week."
    ),
    season: int | None = Query(default=None, ge=1999, le=2100),
    teams: str | None = Query(
        default=None,
        description=(
            "Comma-separated NFL team abbreviations. Defaults to all 32, which is "
            "what makes the archive complete rather than only covering your roster."
        ),
    ),
    refresh: bool = Query(
        default=True,
        description=(
            "Bypass the read caches so the archived line is the one live right now. "
            "Costs one Odds API call per capture."
        ),
    ),
) -> dict[str, Any]:
    """Write this week's odds and injury reports to the append-only archive.

    Meant to be called from a scheduler (Coolify cron), typically once on
    Thursday and once shortly before Sunday kickoff. Rows identical to the last
    recorded state are skipped, so calling it more often than the lines move
    costs nothing but still captures every real change.
    """
    target_season = season or await current_season()
    target_week = week or await current_week_number()

    if teams:
        requested = [normalise_abbr(t) for t in teams.split(",") if t.strip()]
        unknown = [t for t, raw in zip(requested, teams.split(",")) if t is None]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown team abbreviation(s) in '{teams}'.",
            )
        team_list = [t for t in requested if t]
    else:
        team_list = sorted(STADIUMS)

    return await capture_week(
        history,
        odds_provider=odds,
        espn_provider=espn,
        season=target_season,
        week=target_week,
        teams=team_list,
        refresh=refresh,
    )


@app.get(
    "/history",
    tags=["history"],
    dependencies=[Depends(require_api_key)],
    summary="What is in the archive",
)
async def history_inventory() -> dict[str, Any]:
    """Rows, weeks and file sizes per source and season."""
    return {
        "directory": settings.history_path(),
        "sources": list(HISTORY_SOURCES),
        "archived": history.inventory(),
        "note": (
            "Only odds and injury reports are archived. nflverse, Sleeper and "
            "Open-Meteo keep their own history upstream and are re-fetched on demand."
        ),
    }


@app.get(
    "/history/{source}",
    tags=["history"],
    dependencies=[Depends(require_api_key)],
    summary="Read archived rows for one source",
)
async def history_rows(
    source: str = Path(description=f"One of: {', '.join(HISTORY_SOURCES)}."),
    season: int | None = Query(default=None, ge=1999, le=2100),
    week: int | None = Query(default=None, ge=1, le=22),
    limit: int | None = Query(
        default=None, ge=1, le=10000, description="Return only the most recent N rows."
    ),
) -> dict[str, Any]:
    """Every recorded state for a source, oldest first.

    A subject appears more than once when it actually changed - a line that
    moved, or a player who went from limited to full participation - so the
    sequence is the history, not just the latest value.
    """
    if source not in HISTORY_SOURCES:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown history source '{source}'. Valid: {', '.join(HISTORY_SOURCES)}.",
        )
    target_season = season or await current_season()
    rows = history.read(source, target_season, week=week, limit=limit)
    return {
        "source": source,
        "season": target_season,
        "week": week,
        "count": len(rows),
        "rows": rows,
    }


async def _archive_injuries(report: dict[str, Any]) -> None:
    """Archive a team injury report that was just refreshed from ESPN."""
    if not settings.history_auto_capture:
        return
    if not (report.get("cache") or {}).get("refreshed"):
        return
    team = report.get("team")
    injuries = report.get("injuries") or []
    if not team or not injuries:
        return
    season, week = await _current_season_week()
    await auto_capture(history, "injuries", season, week, injury_rows(team, injuries))


# The season and week only change once a week, so one Sleeper call an hour is
# plenty - and it keeps auto-capture from adding a round trip per request.
_SEASON_WEEK_MEMO: dict[str, Any] = {"value": None, "at": 0.0}
_SEASON_WEEK_TTL = 3600.0


async def _current_season_week() -> tuple[int | None, int | None]:
    import time as _time

    if _SEASON_WEEK_MEMO["value"] and (_time.time() - _SEASON_WEEK_MEMO["at"]) < _SEASON_WEEK_TTL:
        return _SEASON_WEEK_MEMO["value"]
    try:
        state = await client.nfl_state()
        value = (int(state.get("season")), services.current_week(state))
    except (HTTPException, TypeError, ValueError) as exc:
        log.warning("Could not resolve the current season/week for auto-capture: %s", exc)
        return (None, None)
    _SEASON_WEEK_MEMO.update(value=value, at=_time.time())
    return value


async def current_week_number() -> int:
    """The week Sleeper considers current."""
    state = await client.nfl_state()
    return services.current_week(state)


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
