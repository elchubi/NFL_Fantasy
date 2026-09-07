"""FastAPI service that exposes a Sleeper fantasy league in readable form.

Every endpoint except /health requires the X-API-Key header. Everything is
read-only: data is pulled live from Sleeper on each request, with only the
~5MB NFL player file cached on disk.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware

from app import __version__, enrichment, services
from app.config import get_settings
from app.backfill import backfill_all, discover_chain
from app.db import Database
from app.history import SOURCES as HISTORY_SOURCES
from app.history import decision_row, outcome_row
from app.managers import build_profiles
from app.players import PlayerStore
from app import playoffs
from app.pressure import analyse_league
from app.public_client import PublicDataClient
from app.security import require_api_key
from app import store
from app.sleeper import SleeperClient
from app.teams import STADIUMS, normalise_abbr

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sleeper-api")

client = SleeperClient()

# Player names, advanced stats, betting lines, injury reports, weather and the
# draft board all live in the shared public-data service now - see
# app/public_client.py and public_data/README.md. PlayerStore still keeps its
# own local disk cache (so roster resolution stays a synchronous, in-memory
# lookup); it is just hydrated from this client instead of from Sleeper
# directly. Shared across every league this backend serves, since player data
# is not per-league.
public = PublicDataClient(
    settings.public_data_url, settings.public_data_api_key, settings.public_data_timeout
)
players = PlayerStore(public)

# One SQLite database per configured league - transactions, draft picks and
# roster snapshots across every season, plus that league's decision log. Kept
# genuinely separate so a bug in one league's data can never touch another's.
databases: dict[str, Database] = {
    slug: Database(settings.database_file(slug)) for slug in settings.league_map
}


def get_db(league: str) -> Database:
    """The database for one configured league slug, or a 404."""
    settings.league_id_for(league)  # raises 404 with the same message if unknown
    return databases[league]


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.league_map:
        log.warning("LEAGUES is not set; every /leagues/{league}/... endpoint will 404.")
    else:
        log.info("Serving %d league(s): %s", len(settings.league_map), ", ".join(sorted(settings.league_map)))
    if not settings.api_key:
        log.warning("API_KEY is not set; protected endpoints will return 503.")
    if not settings.public_data_api_key:
        log.warning(
            "PUBLIC_DATA_API_KEY is not set; every call to the public-data service "
            "will fail with 503, including local player-name resolution."
        )
    # Warm the in-memory copy from disk; the network refresh happens lazily on
    # the first request so a cold public-data service never blocks startup.
    players.load_from_disk()
    # Opens each league's file and applies any pending migrations.
    for slug, database in databases.items():
        log.info("Database ready for '%s': %s", slug, database.stats())
    yield
    await client.aclose()
    await public.aclose()
    for database in databases.values():
        database.close()


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


@app.get("/health", tags=["ops"], summary="Healthcheck for the platform")
async def health() -> dict[str, Any]:
    """This service's own liveness.

    Deliberately does not call the public-data service, so this stays green
    even when that dependency is down - the platform then knows this
    container is fine and the problem is downstream. `health_check` (the
    MCP tool) is the one that proves the whole chain works end to end.
    """
    return {
        "status": "ok",
        "version": __version__,
        "leagues_configured": sorted(settings.league_map),
        "api_key_configured": bool(settings.api_key),
        "players_cache": players.status(),
        "public_data": {
            "url": settings.public_data_url,
            "api_key_configured": bool(settings.public_data_api_key),
        },
    }


@app.get(
    "/leagues",
    tags=["ops"],
    dependencies=[Depends(require_api_key)],
    summary="Which leagues this backend serves",
)
async def leagues_configured() -> dict[str, Any]:
    """The slugs configured in LEAGUES - each one is a `/leagues/{slug}/...` prefix.

    A Sleeper league id is not sensitive, so it is included alongside each slug.
    """
    return {
        "count": len(settings.league_map),
        "leagues": [
            {"slug": slug, "league_id": league_id}
            for slug, league_id in sorted(settings.league_map.items())
        ],
    }


@app.get(
    "/leagues/{league}/snapshot",
    tags=["league"],
    dependencies=[Depends(require_api_key)],
    summary="Whole league state, fully resolved",
)
async def snapshot(
    league: str = Path(description="A slug from GET /leagues."),
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
        settings.league_id_for(league),
        week,
        days,
        includes=enrichment.parse_includes(include),
        public=public,
    )


@app.get(
    "/leagues/{league}/league-settings",
    tags=["league"],
    dependencies=[Depends(require_api_key)],
    summary="League configuration in plain language",
)
async def league_settings(
    league: str = Path(description="A slug from GET /leagues."),
) -> dict[str, Any]:
    """Scoring, roster slots, playoff format, trade deadline and waiver rules."""
    fetched = await client.league(settings.league_id_for(league))
    return services.league_settings_view(fetched)


@app.get(
    "/leagues/{league}/roster/{manager}",
    tags=["league"],
    dependencies=[Depends(require_api_key)],
    summary="One resolved roster, found by username or team name",
)
async def roster(
    league: str = Path(description="A slug from GET /leagues."),
    manager: str = Path(
        description="Username, display name or team name (case-insensitive, partial ok)."
    ),
) -> dict[str, Any]:
    """A single team's roster without pulling the whole league."""
    await players.ensure_fresh()
    lid = settings.league_id_for(league)
    fetched, users, rosters = await asyncio.gather(
        client.league(lid), client.users(lid), client.rosters(lid)
    )

    teams = services.build_teams(users, rosters)
    match = _match_team_or_404(teams, manager)

    raw_roster = next(
        (r for r in rosters if r.get("roster_id") == match["roster_id"]), {}
    )
    resolved = services.resolve_roster(
        raw_roster, match, fetched.get("roster_positions") or [], players
    )
    return {
        "league_id": fetched.get("league_id"),
        "season": fetched.get("season"),
        "matched_on": manager,
        "team": resolved,
        "players_cache": players.status(),
    }


_SKILL_POSITIONS = ("QB", "RB", "WR", "TE")


@app.get(
    "/leagues/{league}/available",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Free agents ranked by role trend and points in this league's own scoring",
)
async def available_players(
    league: str = Path(description="A slug from GET /leagues."),
    position: str | None = Query(
        default=None, description="QB, RB, WR or TE. Omit to check all four."
    ),
    limit: int = Query(default=25, ge=1, le=100, description="Top N per position."),
    season: int | None = Query(default=None, ge=1999, le=2100),
) -> dict[str, Any]:
    """Every rostered-nowhere skill player in this league, ranked by recent
    role trend and points scored under this league's own `scoring_settings` -
    not nflverse's fixed PPR column, which rarely matches a real league's
    rules. This is the list nobody else in the league is looking at: a
    general fantasy tool ranks players against a generic scoring system, not
    against who is actually still on your waiver wire.
    """
    positions = [position.upper()] if position else list(_SKILL_POSITIONS)
    for p in positions:
        if p not in _SKILL_POSITIONS:
            raise HTTPException(
                status_code=400,
                detail=f"'{p}' is not a skill position. Use one of: {', '.join(_SKILL_POSITIONS)}.",
            )

    await players.ensure_fresh()
    lid = settings.league_id_for(league)
    fetched, rosters = await asyncio.gather(client.league(lid), client.rosters(lid))
    scoring_settings = fetched.get("scoring_settings") or {}
    target_season = season or (
        int(fetched["season"]) if fetched.get("season") else await current_season()
    )

    rostered: set[str] = set()
    for roster in rosters:
        rostered.update(str(pid) for pid in (roster.get("players") or []))

    payloads = await asyncio.gather(
        *[
            public.post(
                f"/position-points/{p}",
                params={"season": target_season},
                json={"scoring_settings": scoring_settings},
            )
            for p in positions
        ]
    )

    scoring_not_applied: set[str] = set()
    pool: dict[str, list[dict[str, Any]]] = {}
    for p, payload in zip(positions, payloads):
        scoring_not_applied.update(payload.get("scoring_not_applied") or [])
        candidates: list[dict[str, Any]] = []
        for entry in payload.get("players") or []:
            sleeper_id = players.sleeper_id_for_gsis(entry["gsis_id"])
            if sleeper_id is None or sleeper_id in rostered:
                continue
            resolved = players.resolve(sleeper_id)
            candidates.append(
                {
                    "player_id": sleeper_id,
                    "name": resolved.get("name"),
                    "position": p,
                    "nfl_team": entry.get("team"),
                    "injury_status": resolved.get("injury_status"),
                    "games": entry.get("games"),
                    "season_total_points": entry.get("season_total_points"),
                    "season_average_points": entry.get("season_average_points"),
                    "recent_average_points": entry.get("recent_average_points"),
                }
            )
        candidates.sort(key=lambda c: c["recent_average_points"] or 0, reverse=True)
        pool[p] = candidates[:limit]

    return {
        "season": target_season,
        "rostered_players": len(rostered),
        "scoring_not_applied": sorted(scoring_not_applied),
        "available": pool,
    }


@app.get(
    "/leagues/{league}/schedule-difficulty/{manager}",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="How soft or hard a roster's remaining schedule is, by player",
)
async def schedule_difficulty(
    league: str = Path(description="A slug from GET /leagues."),
    manager: str = Path(description="Username, display name or team name."),
    weeks_ahead: int = Query(
        default=4, ge=1, le=10, description="How many upcoming weeks to check."
    ),
    season: int | None = Query(default=None, ge=1999, le=2100),
) -> dict[str, Any]:
    """For each of a roster's QB/RB/WR/TE, how many fantasy points its next
    few opponents have allowed at that position this season, under this
    league's own scoring rules - not the opponent's real-world defensive
    rank. Useful for a close start/sit or trade-value call between two
    otherwise similar players: the one with the softer slate ahead is worth
    more right now.
    """
    await players.ensure_fresh()
    lid = settings.league_id_for(league)
    state, fetched, users, rosters = await asyncio.gather(
        client.nfl_state(), client.league(lid), client.users(lid), client.rosters(lid)
    )
    teams = services.build_teams(users, rosters)
    match = _match_team_or_404(teams, manager)
    raw_roster = next(
        (r for r in rosters if r.get("roster_id") == match["roster_id"]), {}
    )
    skill_players = [
        resolved
        for pid in (raw_roster.get("players") or [])
        if (resolved := players.resolve(pid)).get("position") in _SKILL_POSITIONS
    ]

    scoring_settings = fetched.get("scoring_settings") or {}
    target_season = season or (
        int(fetched["season"]) if fetched.get("season") else await current_season()
    )
    current = services.current_week(state)
    target_weeks = list(range(current, current + weeks_ahead))
    needed_positions = sorted({p["position"] for p in skill_players})

    schedule_payload, allowed_payloads = await asyncio.gather(
        public.get(f"/schedule/{target_season}"),
        asyncio.gather(
            *[
                public.post(
                    f"/points-allowed/{p}",
                    params={"season": target_season},
                    json={"scoring_settings": scoring_settings},
                )
                for p in needed_positions
            ]
        ),
    )
    opponents_by_team = schedule_payload.get("opponents") or {}
    stinginess_by_position = {
        p: {t["team"]: t for t in payload.get("teams") or []}
        for p, payload in zip(needed_positions, allowed_payloads)
    }

    report = []
    for player in skill_players:
        team = player.get("nfl_team")
        position = player.get("position")
        team_opponents = opponents_by_team.get(team) or {}
        weekly = []
        for week in target_weeks:
            opponent = team_opponents.get(str(week))
            if opponent is None:
                weekly.append({"week": week, "opponent": None, "note": "bye"})
                continue
            defense = stinginess_by_position.get(position, {}).get(opponent)
            weekly.append(
                {
                    "week": week,
                    "opponent": opponent,
                    "average_points_allowed": defense.get("average_points_allowed") if defense else None,
                    "rank_stingiest": defense.get("rank_stingiest") if defense else None,
                }
            )
        report.append(
            {
                "player_id": player.get("player_id"),
                "name": player.get("name"),
                "position": position,
                "nfl_team": team,
                "weeks": weekly,
            }
        )

    return {
        "season": target_season,
        "weeks_checked": target_weeks,
        "matched_on": manager,
        "players": report,
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

    Served by the shared public-data service, since none of this is specific
    to this league.
    """
    return await public.get(f"/advanced-stats/{player_id}", params={"season": season})


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
    The Odds API reports back is included in the response. Served by the
    shared public-data service, which also archives it there - not specific to
    this league, and shared across every league that uses the same key.
    """
    return await public.get(f"/odds/{week}", params={"season": season})


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
    return await public.get("/injury-report", params={"team": team})


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
    return await public.get(f"/injury-report/{player_id}")


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
    return await public.get(f"/weather/{week}", params={"season": season})


@app.get(
    "/stadiums",
    tags=["external"],
    dependencies=[Depends(require_api_key)],
    summary="The static stadium reference used for weather",
)
async def stadiums() -> dict[str, Any]:
    """Coordinates and roof type per team - useful for checking the dome list.

    Pure static data with no upstream fetch, so it is served locally rather
    than round-tripping to the public-data service.
    """
    return {"count": len(STADIUMS), "stadiums": {abbr: dict(meta) for abbr, meta in STADIUMS.items()}}


@app.get(
    "/leagues/{league}/managers",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Behavioural profile of every manager in the league",
)
async def managers(
    league: str = Path(description="A slug from GET /leagues."),
    seasons: str | None = Query(
        default=None,
        description=(
            "Comma-separated seasons to profile, e.g. 2025,2026. Defaults to every "
            "season archived by /backfill, falling back to the current one."
        ),
    ),
    days: int | None = Query(
        default=None,
        ge=1,
        le=4000,
        description=(
            "Only count transactions from the last N days. Omit to use every "
            "archived transaction, which is what makes a multi-season profile."
        ),
    ),
) -> dict[str, Any]:
    """FAAB habits, bid timing, activity, draft tendencies and trade history.

    Derived from your league's own Sleeper history. This is the one thing a
    general fantasy tool cannot do for you: it has no idea who else is in your
    league.

    Reads from the archive, which spans every season `/backfill` has walked -
    each Sleeper season is a separate league, so this is the only way to see
    past ones. Falls back to reading the current season live if nothing is
    archived yet.
    """
    await players.ensure_fresh()
    lid = settings.league_id_for(league)
    db = get_db(league)

    requested = _parse_seasons(seasons)
    archived = await store.known_season_numbers(db)
    target_seasons = requested or archived

    league, users, rosters = await asyncio.gather(
        client.league(lid), client.users(lid), client.rosters(lid)
    )

    since_ms = (time.time() - days * 86400) * 1000 if days else None
    transactions = (
        await store.load_transactions(db, seasons=target_seasons, since_ms=since_ms)
        if target_seasons
        else []
    )
    picks = await store.load_draft_picks(db, target_seasons) if target_seasons else []
    source = "archive"

    if not transactions:
        # Nothing archived yet: read the current season live so the endpoint is
        # useful before the first backfill.
        source = "live (current season only)"
        state = await client.nfl_state()
        weeks = services._weeks_to_scan(services.current_week(state), days or 180)
        pages = await asyncio.gather(*[client.transactions(lid, w) for w in weeks])
        transactions = [tx for page in pages for tx in page]
        drafts = await client.drafts(lid)
        if drafts:
            newest = max(drafts, key=lambda d: str(d.get("season") or ""))
            if newest.get("draft_id"):
                picks = await client.draft_picks(newest["draft_id"])
        target_seasons = [int(league["season"])] if league.get("season") else []

    injury_history: list[dict[str, Any]] = []
    for season in target_seasons:
        try:
            payload = await public.get("/history/injuries", params={"season": season})
            injury_history.extend(payload.get("rows") or [])
        except HTTPException as exc:
            log.warning("Could not read injury history for season %s: %s", season, exc.detail)

    profiles = build_profiles(
        services.build_teams(users, rosters),
        transactions,
        picks,
        players,
        waiver_budget=(league.get("settings") or {}).get("waiver_budget"),
        injury_history=injury_history,
    )
    return {
        "league": league,
        "league_id": lid,
        "source": source,
        "seasons": sorted(target_seasons, reverse=True),
        "seasons_archived": archived,
        "days_filter": days,
        "transactions_read": len(transactions),
        "draft_picks_read": len(picks),
        "injury_history_rows": len(injury_history),
        **profiles,
    }


def _match_team_or_404(teams: dict[Any, dict[str, Any]], manager: str) -> dict[str, Any]:
    """The one team `manager` matches, or a 404/409 shaped like every other
    manager-lookup endpoint here."""
    match, candidates = services.find_team(teams, manager)
    if match is not None:
        return match
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


def _parse_seasons(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"'{part}' is not a season year."
            ) from None
    return out


@app.get(
    "/leagues/{league}/manager/{name}",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Behavioural profile of one manager",
)
async def manager(
    league: str = Path(description="A slug from GET /leagues."),
    name: str = Path(description="Username, display name or team name."),
    seasons: str | None = Query(default=None),
    days: int | None = Query(default=None, ge=1, le=4000),
) -> dict[str, Any]:
    """One manager's profile, read against the rest of the league."""
    everyone = await managers(league=league, seasons=seasons, days=days)
    needle = " ".join(name.strip().lower().split())

    match = next(
        (
            profile
            for profile in everyone["managers"]
            if needle
            in " ".join(
                str(v).lower()
                for v in (profile.get("display_name"), profile.get("team_name"))
                if v
            )
        ),
        None,
    )
    if match is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"No manager matches '{name}'.",
                "available": [
                    {"display_name": p["display_name"], "team_name": p["team_name"]}
                    for p in everyone["managers"]
                ],
            },
        )
    return {
        "manager": match,
        "league_context": everyone["league_context"],
        "seasons": everyone["seasons"],
        "source": everyone["source"],
    }


@app.get(
    "/leagues/{league}/pressure",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Which teams are structurally forced to act",
)
async def pressure(
    league: str = Path(description="A slug from GET /leagues."),
    week: int | None = Query(default=None, ge=1, le=22),
    horizon: int = Query(
        default=3, ge=1, le=6, description="How many weeks ahead to look."
    ),
) -> dict[str, Any]:
    """Bye-week collisions, stacked injuries and positions with no cover.

    A team that has to move before you do is a team you have leverage over.
    """
    await players.ensure_fresh()
    lid = settings.league_id_for(league)
    state, fetched_league, users, rosters = await asyncio.gather(
        client.nfl_state(), client.league(lid), client.users(lid), client.rosters(lid)
    )

    target_week = week or services.current_week(state)
    season = services._season_number(state, fetched_league)
    roster_positions = fetched_league.get("roster_positions") or []
    teams = services.build_teams(users, rosters)

    byes_response = await public.get(f"/byes/{season}") if season else {}
    byes = byes_response.get("byes") or {}
    resolved = [
        services.resolve_roster(r, teams.get(r.get("roster_id"), {}), roster_positions, players)
        for r in rosters
    ]

    report = analyse_league(resolved, roster_positions, byes, target_week, horizon=horizon)
    return {
        "season": season,
        "bye_weeks_known": bool(byes),
        **report,
    }


@app.get(
    "/leagues/{league}/playoff-odds",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Monte Carlo playoff odds and a buy/sell read per team",
)
async def playoff_odds(
    league: str = Path(description="A slug from GET /leagues."),
    trials: int = Query(
        default=3000, ge=100, le=20000, description="Simulated seasons to run."
    ),
) -> dict[str, Any]:
    """Simulates the rest of the regular season from each team's own scoring
    history (mean and spread of its own points_for so far - not a projection
    system, not opponent-specific) to estimate each team's odds of making the
    playoffs.

    A team's odds cross with its roster to say something a raw record can't:
    an 8-2 team already locked into the playoffs is a soft trade target for a
    win-now player, while a 3-7 team with a strong roster and long playoff
    odds should be selling. Treats the current week as still undecided, which
    slightly understates a team whose current-week result has already landed
    but not yet been read back from Sleeper.
    """
    lid = settings.league_id_for(league)
    state, fetched, users, rosters = await asyncio.gather(
        client.nfl_state(), client.league(lid), client.users(lid), client.rosters(lid)
    )
    teams = services.build_teams(users, rosters)
    league_settings = fetched.get("settings") or {}
    playoff_spots = int(league_settings.get("playoff_teams") or 6)
    playoff_start = int(league_settings.get("playoff_week_start") or 15)
    current = services.current_week(state)

    weeks_played = list(range(1, current))
    weeks_remaining = [w for w in range(current, playoff_start) if w >= 1]

    history_pages, remaining_pages = await asyncio.gather(
        asyncio.gather(*[client.matchups(lid, w) for w in weeks_played]),
        asyncio.gather(*[client.matchups(lid, w) for w in weeks_remaining]),
    )

    weekly_scores: dict[int, list[float]] = {rid: [] for rid in teams}
    for page in history_pages:
        for entry in page:
            rid = entry.get("roster_id")
            points = entry.get("points")
            if rid in weekly_scores and points:
                weekly_scores[rid].append(float(points))

    remaining_matchups: list[list[tuple[int, int]]] = []
    for page in remaining_pages:
        by_matchup: dict[Any, list[int]] = {}
        for entry in page:
            matchup_id = entry.get("matchup_id")
            rid = entry.get("roster_id")
            if matchup_id is not None and rid is not None:
                by_matchup.setdefault(matchup_id, []).append(rid)
        remaining_matchups.append(
            [tuple(pair) for pair in by_matchup.values() if len(pair) == 2]
        )

    standings = {
        rid: {
            "wins": team["record"]["wins"],
            "losses": team["record"]["losses"],
            "points_for": team["record"]["points_for"],
        }
        for rid, team in teams.items()
    }
    profiles = playoffs.team_scoring_profiles(weekly_scores)
    odds = playoffs.simulate_playoff_odds(
        profiles, standings, remaining_matchups, playoff_spots, trials=trials
    )

    report = [
        {
            "roster_id": rid,
            "display_name": team["display_name"],
            "team_name": team["team_name"],
            "record": team["record"],
            "scoring_profile": profiles.get(rid),
            "playoff_odds": odds.get(rid, 0.0),
            "read": playoffs.classify(odds.get(rid, 0.0)),
        }
        for rid, team in teams.items()
    ]
    report.sort(key=lambda t: t["playoff_odds"], reverse=True)

    return {
        "current_week": current,
        "playoff_week_start": playoff_start,
        "playoff_spots": playoff_spots,
        "weeks_simulated": weeks_remaining,
        "trials": trials,
        "teams": report,
    }


@app.post(
    "/leagues/{league}/decision",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Log a decision you made, and why",
)
async def log_decision(
    league: str = Path(description="A slug from GET /leagues."),
    kind: str = Query(description="waiver_bid, trade, start_sit, draft_pick, keeper, drop, other."),
    summary: str = Query(description="What you decided, in one line."),
    reasoning: str | None = Query(default=None, description="Why you decided it."),
    players_involved: str | None = Query(
        default=None, description="Comma-separated player names."
    ),
    confidence: str | None = Query(default=None, description="e.g. low / medium / high."),
    expected: str | None = Query(default=None, description="What you expect to happen."),
    week: int | None = Query(default=None, ge=1, le=22),
    season: int | None = Query(default=None, ge=1999, le=2100),
) -> dict[str, Any]:
    """Append a decision to the log.

    Recorded at the moment you make it, before you know how it turned out -
    which is the only version worth having later.
    """
    db = get_db(league)
    target_season = season or await current_season()
    target_week = week or await current_week_number()
    row = decision_row(
        kind=kind,
        summary=summary,
        reasoning=reasoning,
        players=[p.strip() for p in (players_involved or "").split(",") if p.strip()],
        confidence=confidence,
        expected=expected,
    )
    result = await store.append_decision(db, target_season, target_week, row)
    return {"logged": result.get("written", 0) == 1, "decision": row, "archive": result}


@app.post(
    "/leagues/{league}/decision/{decision_id}/outcome",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Record how a logged decision turned out",
)
async def log_outcome(
    league: str = Path(description="A slug from GET /leagues."),
    decision_id: str = Path(description="The decision_id returned by POST /decision."),
    outcome: str = Query(description="What actually happened."),
    season: int | None = Query(default=None, ge=1999, le=2100),
) -> dict[str, Any]:
    """Append the outcome. The original call is never edited, only layered on."""
    db = get_db(league)
    target_season = season or await current_season()
    original = await store.find_decision(db, target_season, decision_id)
    if original is None:
        raise HTTPException(
            status_code=404,
            detail=f"No decision '{decision_id}' logged in {target_season}.",
        )
    follow_up = outcome_row(original, outcome)
    result = await store.append_decision(db, target_season, original.get("week"), follow_up)
    return {"recorded": True, "decision": follow_up, "archive": result}


@app.get(
    "/leagues/{league}/decisions",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Read the decision log",
)
async def decisions(
    league: str = Path(description="A slug from GET /leagues."),
    season: int | None = Query(default=None, ge=1999, le=2100),
    week: int | None = Query(default=None, ge=1, le=22),
    kind: str | None = Query(default=None, description="Filter by decision kind."),
    pending_only: bool = Query(
        default=False, description="Only decisions with no outcome recorded yet."
    ),
) -> dict[str, Any]:
    """Your decisions with their outcomes, oldest first."""
    db = get_db(league)
    target_season = season or await current_season()
    rows = await store.read_decisions(db, target_season, week=week)
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    if pending_only:
        rows = [r for r in rows if not r.get("outcome")]

    resolved = [r for r in rows if r.get("outcome")]
    return {
        "season": target_season,
        "count": len(rows),
        "with_outcome": len(resolved),
        "awaiting_outcome": len(rows) - len(resolved),
        "decisions": rows,
    }


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

    Built from nflverse's draft_picks and combine releases, not specific to
    this league, so served by the shared public-data service.
    """
    return await public.get(
        f"/draft-class/{season}",
        params={"position": position, "round_max": round_max, "landing": landing},
    )


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
    return await public.get(f"/prospect/{player_id}", params={"season": season})


@app.post(
    "/leagues/{league}/backfill",
    tags=["history"],
    dependencies=[Depends(require_api_key)],
    summary="Archive every season of the league",
)
async def backfill(
    league: str = Path(description="A slug from GET /leagues."),
    refresh: bool = Query(
        default=False,
        description=(
            "Re-read seasons already archived. A finished season cannot change, "
            "so this is only useful after a bug fix."
        ),
    ),
    limit: int = Query(
        default=20, ge=1, le=20, description="How many seasons back to walk."
    ),
) -> dict[str, Any]:
    """Walk `previous_league_id` and archive each season's history.

    In Sleeper every season is a separate league, so a single Sleeper league id
    only ever reaches the current season. This follows the chain backwards and
    stores transactions, draft picks, managers and final rosters for each
    season it finds. Run it once after deploying, then whenever a season ends.

    Finished seasons are skipped on a re-run since they cannot change; the
    season in progress is always re-read.
    """
    return await backfill_all(
        client, get_db(league), settings.league_id_for(league), refresh=refresh, limit=limit
    )


@app.get(
    "/leagues/{league}/seasons",
    tags=["history"],
    dependencies=[Depends(require_api_key)],
    summary="The league's season chain",
)
async def seasons_endpoint(
    league: str = Path(description="A slug from GET /leagues."),
    discover: bool = Query(
        default=False,
        description="Follow previous_league_id live instead of reading the archive.",
    ),
) -> dict[str, Any]:
    """Every season of this league, newest first.

    Reads the archive by default. `discover=true` walks the chain against
    Sleeper, which is how to see what a backfill would pick up before running it.
    """
    if discover:
        chain = await discover_chain(client, settings.league_id_for(league))
        return {
            "source": "sleeper",
            "seasons": [
                {
                    "season": entry.get("season"),
                    "league_id": entry.get("league_id"),
                    "name": entry.get("name"),
                    "status": entry.get("status"),
                    "previous_league_id": entry.get("previous_league_id"),
                }
                for entry in chain
            ],
        }
    return {"source": "archive", "seasons": await store.seasons(get_db(league))}


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

    Meant to be called from a scheduler (a Railway cron service), typically once on
    Thursday and once shortly before Sunday kickoff. Rows identical to the last
    recorded state are skipped, so calling it more often than the lines move
    costs nothing but still captures every real change.

    Odds and injury reports are not specific to this league, so the archive
    itself lives in the shared public-data service; this just forwards to it.
    """
    return await public.post(
        "/capture", params={"week": week, "season": season, "teams": teams, "refresh": refresh}
    )


@app.get(
    "/leagues/{league}/history",
    tags=["history"],
    dependencies=[Depends(require_api_key)],
    summary="What is in the archive",
)
async def history_inventory(
    league: str = Path(description="A slug from GET /leagues."),
) -> dict[str, Any]:
    """Rows, weeks and file sizes per source and season.

    Merges this league's own archive (transactions, draft picks, roster
    snapshots, decisions) with the odds/injury archive from the shared
    public-data service.
    """
    db = get_db(league)
    league_stats, public_history = await asyncio.gather(
        store.inventory(db), public.get("/history")
    )
    return {
        "database": db.stats(),
        "archived": {**league_stats, **(public_history.get("archived") or {})},
        "seasons": await store.seasons(db),
        "note": (
            "Transactions, draft picks and roster snapshots come from /backfill. "
            "Odds and injuries are archived by the shared public-data service, "
            "since they are not specific to this league."
        ),
    }


@app.get(
    "/leagues/{league}/history/{source}",
    tags=["history"],
    dependencies=[Depends(require_api_key)],
    summary="Read archived rows for one source",
)
async def history_rows(
    league: str = Path(description="A slug from GET /leagues."),
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
    sequence is the history, not just the latest value. `odds` and `injuries`
    are proxied to the shared public-data service; `decisions` is this
    league's own.
    """
    if source not in HISTORY_SOURCES:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown history source '{source}'. Valid: {', '.join(HISTORY_SOURCES)}.",
        )
    if source in ("odds", "injuries"):
        return await public.get(
            f"/history/{source}", params={"season": season, "week": week, "limit": limit}
        )
    db = get_db(league)
    target_season = season or await current_season()
    rows = await store.read_decisions(db, target_season, week=week)
    rows = rows[-limit:] if limit else rows
    return {
        "source": source,
        "season": target_season,
        "week": week,
        "count": len(rows),
        "rows": rows,
    }


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
