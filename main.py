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
# directly.
public = PublicDataClient(
    settings.public_data_url, settings.public_data_api_key, settings.public_data_timeout
)
players = PlayerStore(public)

# The league's own history: transactions, draft picks and roster snapshots
# across every season in the chain, plus the decision log.
db = Database(settings.database_file())


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.league_id:
        log.warning("LEAGUE_ID is not set; league endpoints will return 503.")
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
    # Opens the file and applies any pending migrations.
    log.info("Database ready: %s", db.stats())
    yield
    await client.aclose()
    await public.aclose()
    db.close()


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
        "league_id_configured": bool(settings.league_id),
        "api_key_configured": bool(settings.api_key),
        "players_cache": players.status(),
        "public_data": {
            "url": settings.public_data_url,
            "api_key_configured": bool(settings.public_data_api_key),
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
        public=public,
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
    "/managers",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Behavioural profile of every manager in the league",
)
async def managers(
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
    lid = league_id()

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
        injury_history.extend(await store.read_injuries(db, season))

    profiles = build_profiles(
        services.build_teams(users, rosters),
        transactions,
        picks,
        players,
        waiver_budget=(league.get("settings") or {}).get("waiver_budget"),
        injury_history=injury_history,
    )
    return {
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
    "/manager/{name}",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Behavioural profile of one manager",
)
async def manager(
    name: str = Path(description="Username, display name or team name."),
    seasons: str | None = Query(default=None),
    days: int | None = Query(default=None, ge=1, le=4000),
) -> dict[str, Any]:
    """One manager's profile, read against the rest of the league."""
    everyone = await managers(seasons=seasons, days=days)
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
    "/pressure",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Which teams are structurally forced to act",
)
async def pressure(
    week: int | None = Query(default=None, ge=1, le=22),
    horizon: int = Query(
        default=3, ge=1, le=6, description="How many weeks ahead to look."
    ),
) -> dict[str, Any]:
    """Bye-week collisions, stacked injuries and positions with no cover.

    A team that has to move before you do is a team you have leverage over.
    """
    await players.ensure_fresh()
    lid = league_id()
    state, league, users, rosters = await asyncio.gather(
        client.nfl_state(), client.league(lid), client.users(lid), client.rosters(lid)
    )

    target_week = week or services.current_week(state)
    season = services._season_number(state, league)
    roster_positions = league.get("roster_positions") or []
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


@app.post(
    "/decision",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Log a decision you made, and why",
)
async def log_decision(
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
    "/decision/{decision_id}/outcome",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Record how a logged decision turned out",
)
async def log_outcome(
    decision_id: str = Path(description="The decision_id returned by POST /decision."),
    outcome: str = Query(description="What actually happened."),
    season: int | None = Query(default=None, ge=1999, le=2100),
) -> dict[str, Any]:
    """Append the outcome. The original call is never edited, only layered on."""
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
    "/decisions",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Read the decision log",
)
async def decisions(
    season: int | None = Query(default=None, ge=1999, le=2100),
    week: int | None = Query(default=None, ge=1, le=22),
    kind: str | None = Query(default=None, description="Filter by decision kind."),
    pending_only: bool = Query(
        default=False, description="Only decisions with no outcome recorded yet."
    ),
) -> dict[str, Any]:
    """Your decisions with their outcomes, oldest first."""
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
    "/backfill",
    tags=["history"],
    dependencies=[Depends(require_api_key)],
    summary="Archive every season of the league",
)
async def backfill(
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

    In Sleeper every season is a separate league, so a single LEAGUE_ID only
    ever reaches the current one. This follows the chain backwards and stores
    transactions, draft picks, managers and final rosters for each season it
    finds. Run it once after deploying, then whenever a season ends.

    Finished seasons are skipped on a re-run since they cannot change; the
    season in progress is always re-read.
    """
    return await backfill_all(client, db, league_id(), refresh=refresh, limit=limit)


@app.get(
    "/seasons",
    tags=["history"],
    dependencies=[Depends(require_api_key)],
    summary="The league's season chain",
)
async def seasons_endpoint(
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
        chain = await discover_chain(client, league_id())
        return {
            "source": "sleeper",
            "seasons": [
                {
                    "season": league.get("season"),
                    "league_id": league.get("league_id"),
                    "name": league.get("name"),
                    "status": league.get("status"),
                    "previous_league_id": league.get("previous_league_id"),
                }
                for league in chain
            ],
        }
    return {"source": "archive", "seasons": await store.seasons(db)}


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
    "/history",
    tags=["history"],
    dependencies=[Depends(require_api_key)],
    summary="What is in the archive",
)
async def history_inventory() -> dict[str, Any]:
    """Rows, weeks and file sizes per source and season.

    Merges this league's own archive (transactions, draft picks, roster
    snapshots, decisions) with the odds/injury archive from the shared
    public-data service.
    """
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
