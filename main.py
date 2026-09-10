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
from app.pressure import analyse_league, positional_balance
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

# Sleeper's own vocabulary for "still a rosterable NFL player right now".
# Retired and long-unsigned players sit in the player file forever with no
# status at all (or "Inactive"), rather than being removed - excluding those
# is what keeps /available from recommending someone who last played years ago.
_ROSTERABLE_STATUSES = frozenset(
    {"Active", "Injured Reserve", "PUP", "Non Football Injury", "Suspended", "Practice Squad"}
)


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
            # Sleeper's player file keeps long-retired and unsigned players
            # indefinitely with no current status - nflverse still carries
            # their old stat lines under the same gsis_id from whichever
            # season it fell back to, so without this check a real "waiver
            # pickup" list could recommend someone who last played years ago.
            if resolved.get("status") not in _ROSTERABLE_STATUSES:
                continue
            # A player with no current NFL team cannot actually be added off
            # waivers, whatever `status` says - confirmed live: Tyreek Hill's
            # own record reports status "Active" with nfl_team null and an
            # ACL surgery note, which the status check alone does not catch.
            if not resolved.get("nfl_team"):
                continue
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


_MAX_COMPARE_PLAYERS = 20


@app.get(
    "/leagues/{league}/players/compare",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Real production for any list of players, rostered or not",
)
async def players_compare(
    league: str = Path(description="A slug from GET /leagues."),
    ids: str | None = Query(default=None, description="Comma-separated Sleeper player ids."),
    names: str | None = Query(
        default=None,
        description=(
            "Comma-separated player names. Flexible, case-insensitive: exact "
            "match first, then prefix, then substring - same three-tier lookup "
            "league_roster uses for managers."
        ),
    ),
    season: int | None = Query(default=None, ge=1999, le=2100),
) -> dict[str, Any]:
    """The same production numbers `available` ranks free agents by - games,
    season_total_points, season_average_points, recent_average_points -
    generalized to any list of players, rostered or not. `available` only
    covers the current free-agent pool, so it can't help with a trade
    evaluation (both sides are rostered) or a live-draft comparison where
    some options are already gone; this can.

    Pass `ids`, `names`, or both - up to `_MAX_COMPARE_PLAYERS` players total,
    to keep the response from ballooning. A player that doesn't resolve, is
    an ambiguous name, or plays a position with no production data (only
    QB/RB/WR/TE are covered here, same as `available`) is reported in
    `unresolved` rather than failing the whole request. A player who does
    resolve but has no stat row yet this season (a bye, an injury, or simply
    a game that hasn't kicked off yet) still comes back in `players`, with
    `games: 0` and the same "no sample" convention as the rest of this API:
    `season_total_points: 0`, the averages `null`.
    """
    requested_ids = [v.strip() for v in (ids or "").split(",") if v.strip()]
    requested_names = [v.strip() for v in (names or "").split(",") if v.strip()]
    if not requested_ids and not requested_names:
        raise HTTPException(
            status_code=400, detail="Provide at least one player via 'ids' or 'names'."
        )
    total_requested = len(requested_ids) + len(requested_names)
    if total_requested > _MAX_COMPARE_PLAYERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Up to {_MAX_COMPARE_PLAYERS} players per call; got {total_requested}. "
                "Split the comparison into more than one call."
            ),
        )

    await players.ensure_fresh()
    lid = settings.league_id_for(league)
    fetched = await client.league(lid)
    scoring_settings = fetched.get("scoring_settings") or {}
    target_season = season or (
        int(fetched["season"]) if fetched.get("season") else await current_season()
    )

    wanted: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []

    for pid in requested_ids:
        resolved = players.resolve(pid)
        if not resolved.get("resolved"):
            unresolved.append({"query": pid, "reason": f"'{pid}' is not a known Sleeper player id."})
            continue
        wanted[resolved["player_id"]] = resolved

    for name in requested_names:
        match, candidates = players.find_by_query(name)
        if match is None:
            reason = (
                f"'{name}' matches more than one player." if candidates
                else f"No player matches '{name}'."
            )
            entry = {"query": name, "reason": reason}
            if candidates:
                entry["candidates"] = candidates
            unresolved.append(entry)
            continue
        wanted[match] = players.resolve(match)

    by_position: dict[str, list[str]] = {}
    for pid, resolved in wanted.items():
        position = (resolved.get("position") or "").upper()
        if position not in _SKILL_POSITIONS:
            unresolved.append(
                {
                    "query": pid,
                    "reason": (
                        f"{resolved.get('name')} plays "
                        f"{position or 'an unknown position'}; only "
                        f"{', '.join(_SKILL_POSITIONS)} have production data."
                    ),
                }
            )
            continue
        by_position.setdefault(position, []).append(pid)

    if not by_position:
        return {
            "season": target_season,
            "scoring_not_applied": [],
            "players": [],
            "unresolved": unresolved,
        }

    payloads = await asyncio.gather(
        *[
            public.post(
                f"/position-points/{p}",
                params={"season": target_season},
                json={"scoring_settings": scoring_settings},
            )
            for p in by_position
        ]
    )

    scoring_not_applied: set[str] = set()
    entries_by_pid: dict[str, dict[str, Any]] = {}
    for position, payload in zip(by_position, payloads):
        scoring_not_applied.update(payload.get("scoring_not_applied") or [])
        still_wanted = set(by_position[position])
        for entry in payload.get("players") or []:
            sleeper_id = players.sleeper_id_for_gsis(entry["gsis_id"])
            if sleeper_id in still_wanted:
                entries_by_pid[sleeper_id] = entry

    output = []
    for pid, resolved in wanted.items():
        entry = entries_by_pid.get(pid)
        # A skill-position player is only absent from position-points when
        # nflverse has no stat row for them yet this season - a bye, an
        # injury, or (this week) a game that just hasn't kicked off. That is
        # real, reportable information about this specific player, not a
        # failure to resolve them, so it is zero production in `players`
        # (same "no sample" convention public_data's own scoring.py uses:
        # season_total_points 0, the averages None) rather than `unresolved`.
        output.append(
            {
                "player_id": pid,
                "name": resolved.get("name"),
                "position": resolved.get("position"),
                "nfl_team": (entry or {}).get("team") or resolved.get("nfl_team"),
                "injury_status": resolved.get("injury_status"),
                "games": entry.get("games") if entry else 0,
                "season_total_points": entry.get("season_total_points") if entry else 0.0,
                "season_average_points": entry.get("season_average_points") if entry else None,
                "recent_average_points": entry.get("recent_average_points") if entry else None,
            }
        )

    return {
        "season": target_season,
        "scoring_not_applied": sorted(scoring_not_applied),
        "players": output,
        "unresolved": unresolved,
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
    "/leagues/{league}/schedule/{manager}",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="One manager's opponent for every remaining regular-season week",
)
async def manager_schedule(
    league: str = Path(description="A slug from GET /leagues."),
    manager: str = Path(description="Username, display name or team name."),
) -> dict[str, Any]:
    """Who `manager` plays each week of the regular season, from Sleeper's own
    pre-generated pairing (available for future weeks too, not just ones
    already played - the same mechanism /playoff-odds uses to simulate the
    rest of the season).

    This is the pairing only, not a strength read - a team's record and
    playoff odds mean little before real games have been played, so cross
    this with /playoff-odds yourself once there is a few weeks of results to
    judge an opponent by, rather than trusting week-1 odds that are close to
    a coin flip for everyone.
    """
    lid = settings.league_id_for(league)
    fetched, users, rosters = await asyncio.gather(
        client.league(lid), client.users(lid), client.rosters(lid)
    )
    teams = services.build_teams(users, rosters)
    match = _match_team_or_404(teams, manager)

    playoff_start = int((fetched.get("settings") or {}).get("playoff_week_start") or 15)
    weeks = list(range(1, playoff_start))
    pages = await asyncio.gather(*[client.matchups(lid, w) for w in weeks])

    schedule = []
    for week, rows in zip(weeks, pages):
        mine = next((r for r in rows if r.get("roster_id") == match["roster_id"]), None)
        matchup_id = mine.get("matchup_id") if mine else None
        opponent_row = next(
            (
                r
                for r in rows
                if r.get("matchup_id") == matchup_id and r.get("roster_id") != match["roster_id"]
            ),
            None,
        ) if matchup_id is not None else None

        schedule.append(
            {
                "week": week,
                "bye": opponent_row is None,
                "opponent": services.team_label(teams, opponent_row["roster_id"])
                if opponent_row
                else None,
            }
        )

    return {
        "matched_on": manager,
        "team": services.team_label(teams, match["roster_id"]),
        "playoff_week_start": playoff_start,
        "schedule": schedule,
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


async def _draft_picks_for_season(
    lid: str, db: Database, season: int
) -> tuple[list[dict[str, Any]], str]:
    """One season's draft picks: the archive /backfill has walked, falling
    back to the live current-season draft if nothing has been archived yet -
    the same two-path behaviour /managers already uses for this exact data,
    reused here rather than re-fetched."""
    picks = await store.load_draft_picks(db, [season])
    if picks:
        return picks, "archive"

    drafts = await client.drafts(lid)
    if drafts:
        newest = max(drafts, key=lambda d: str(d.get("season") or ""))
        if newest.get("draft_id"):
            return await client.draft_picks(newest["draft_id"]), "live (current season only)"
    return [], "live (current season only)"


@app.get(
    "/leagues/{league}/draft-picks/{manager}",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="One manager's draft, pick by pick",
)
async def draft_picks_for_manager(
    league: str = Path(description="A slug from GET /leagues."),
    manager: str = Path(description="Username, display name or team name."),
    season: int | None = Query(default=None, ge=1999, le=2100),
) -> dict[str, Any]:
    """Every pick `manager` made in one season's draft, in draft order.

    This is the same per-pick data /managers already aggregates into
    `positions_taken` and `average_round_by_position` - just not rolled up,
    for anyone who wants to see the actual picks rather than the summary.
    """
    await players.ensure_fresh()
    lid = settings.league_id_for(league)
    db = get_db(league)

    fetched, users, rosters = await asyncio.gather(
        client.league(lid), client.users(lid), client.rosters(lid)
    )
    teams = services.build_teams(users, rosters)
    match = _match_team_or_404(teams, manager)

    target_season = season or (
        int(fetched["season"]) if fetched.get("season") else await current_season()
    )
    picks, source = await _draft_picks_for_season(lid, db, target_season)
    mine = sorted(
        (p for p in picks if p.get("roster_id") == match["roster_id"]),
        key=lambda p: p.get("pick_no") or 999,
    )

    return {
        "matched_on": manager,
        "team": services.team_label(teams, match["roster_id"]),
        "season": target_season,
        "source": source,
        "picks": [
            {
                "round": p.get("round"),
                "pick_no": p.get("pick_no"),
                "player": players.resolve(p.get("player_id")),
            }
            for p in mine
        ],
    }


@app.get(
    "/leagues/{league}/draft-board",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="The whole league's draft, pick by pick",
)
async def draft_board(
    league: str = Path(description="A slug from GET /leagues."),
    season: int | None = Query(default=None, ge=1999, le=2100),
) -> dict[str, Any]:
    """Every pick from one season's draft, across every team, in overall pick
    order - the same per-pick data behind /managers' draft aggregates, laid
    out as the whole board rather than split per manager.
    """
    await players.ensure_fresh()
    lid = settings.league_id_for(league)
    db = get_db(league)

    fetched, users, rosters = await asyncio.gather(
        client.league(lid), client.users(lid), client.rosters(lid)
    )
    teams = services.build_teams(users, rosters)

    target_season = season or (
        int(fetched["season"]) if fetched.get("season") else await current_season()
    )
    picks, source = await _draft_picks_for_season(lid, db, target_season)
    ordered = sorted(picks, key=lambda p: p.get("pick_no") or 999)

    return {
        "season": target_season,
        "source": source,
        "picks": [
            {
                "round": p.get("round"),
                "pick_no": p.get("pick_no"),
                "team": services.team_label(teams, p.get("roster_id")),
                "player": players.resolve(p.get("player_id")),
            }
            for p in ordered
        ],
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


async def _simulate_playoff_odds(
    lid: str,
    fetched: dict[str, Any],
    teams: dict[Any, dict[str, Any]],
    current: int,
    trials: int = 1500,
) -> tuple[dict[Any, float], dict[Any, dict[str, float]], int, list[int]]:
    """Shared by /playoff-odds and /trade-fits: each roster's playoff odds and
    scoring profile, plus the playoff start week and the weeks simulated."""
    league_settings = fetched.get("settings") or {}
    playoff_spots = int(league_settings.get("playoff_teams") or 6)
    playoff_start = int(league_settings.get("playoff_week_start") or 15)

    weeks_played = list(range(1, current))
    weeks_remaining = [w for w in range(current, playoff_start) if w >= 1]

    history_pages, remaining_pages = await asyncio.gather(
        asyncio.gather(*[client.matchups(lid, w) for w in weeks_played]),
        asyncio.gather(*[client.matchups(lid, w) for w in weeks_remaining]),
    )

    weekly_scores: dict[Any, list[float]] = {rid: [] for rid in teams}
    for page in history_pages:
        for entry in page:
            rid = entry.get("roster_id")
            points = entry.get("points")
            if rid in weekly_scores and points:
                weekly_scores[rid].append(float(points))

    remaining_matchups: list[list[tuple[Any, Any]]] = []
    for page in remaining_pages:
        by_matchup: dict[Any, list[Any]] = {}
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
    return odds, profiles, playoff_start, weeks_remaining


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
    current = services.current_week(state)
    odds, profiles, playoff_start, weeks_remaining = await _simulate_playoff_odds(
        lid, fetched, teams, current, trials=trials
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
        "playoff_spots": int((fetched.get("settings") or {}).get("playoff_teams") or 6),
        "weeks_simulated": weeks_remaining,
        "trials": trials,
        "teams": report,
    }


@app.get(
    "/leagues/{league}/trade-fits/{manager}",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="Who to approach for a trade, and about what position",
)
async def trade_fits(
    league: str = Path(description="A slug from GET /leagues."),
    manager: str = Path(description="Username, display name or team name."),
) -> dict[str, Any]:
    """Crosses your positional deficits against every other team's surplus at
    that position, weighted by their playoff odds and their trade history
    with you, to shortlist who to approach and about what.

    A deficit here is a position with no spare healthy body beyond the
    starters it fills (the same read /pressure uses); a surplus is the
    opposite - extra healthy depth nobody else can see because a raw record
    or roster listing does not compute it. Sellers (long playoff odds) are
    ranked first, since they are the likeliest to actually move a surplus
    player rather than sit on him as insurance.
    """
    await players.ensure_fresh()
    lid = settings.league_id_for(league)
    state, fetched, users, rosters = await asyncio.gather(
        client.nfl_state(), client.league(lid), client.users(lid), client.rosters(lid)
    )
    teams = services.build_teams(users, rosters)
    match = _match_team_or_404(teams, manager)
    roster_positions = fetched.get("roster_positions") or []
    resolved_by_roster = {
        r.get("roster_id"): services.resolve_roster(
            r, teams.get(r.get("roster_id"), {}), roster_positions, players
        )
        for r in rosters
    }

    my_balance = positional_balance(resolved_by_roster[match["roster_id"]], roster_positions)
    my_deficits = [b["position"] for b in my_balance if b["spare"] <= 0]
    if not my_deficits:
        return {
            "matched_on": manager,
            "your_deficits": [],
            "candidates": [],
            "note": "No thin positions right now - nothing urgent to trade for.",
        }

    current = services.current_week(state)
    odds, _profiles, _playoff_start, _weeks = await _simulate_playoff_odds(
        lid, fetched, teams, current, trials=1500
    )
    manager_data = await managers(league=league, seasons=None, days=None)
    trade_partners_by_roster = {
        m["roster_id"]: m.get("trade_partners") or {} for m in manager_data.get("managers", [])
    }
    my_trade_partners = trade_partners_by_roster.get(match["roster_id"], {})

    candidates = []
    for rid, team in teams.items():
        if rid == match["roster_id"]:
            continue
        their_balance = {
            b["position"]: b for b in positional_balance(resolved_by_roster[rid], roster_positions)
        }
        fits = [
            {"position": position, "their_spare": their_balance[position]["spare"]}
            for position in my_deficits
            if position in their_balance and their_balance[position]["spare"] > 0
        ]
        if not fits:
            continue
        their_odds = odds.get(rid, 0.0)
        candidates.append(
            {
                "roster_id": rid,
                "display_name": team["display_name"],
                "team_name": team["team_name"],
                "playoff_odds": their_odds,
                "read": playoffs.classify(their_odds),
                "positions_they_can_fill_for_you": fits,
                "past_trades_with_you": my_trade_partners.get(str(rid), 0),
            }
        )
    candidates.sort(
        key=lambda c: (
            c["read"] == "seller",
            len(c["positions_they_can_fill_for_you"]),
            c["past_trades_with_you"],
        ),
        reverse=True,
    )

    return {
        "matched_on": manager,
        "your_deficits": my_deficits,
        "candidates": candidates,
    }


@app.get(
    "/leagues/{league}/faab-bid/{manager}",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="A FAAB bid recommendation, based on this league's own history",
)
async def faab_bid(
    league: str = Path(description="A slug from GET /leagues."),
    manager: str = Path(description="Your own team - username, display name or team name."),
    player_id: str | None = Query(
        default=None, description="Sleeper player id, for a labelled response."
    ),
    confidence: str = Query(
        default="medium",
        description="low, medium or high - how much you want this player.",
    ),
) -> dict[str, Any]:
    """Recommends a bid against this league's own remaining budgets and
    bidding history, not a generic 'bid $X for a WR2' rule.

    Takes the most dangerous rival - the manager with both a track record of
    bidding high and enough remaining budget to actually do it again - as the
    baseline, then adds a margin that scales with how much you want the
    player. There is no signal here about who else actually wants this
    specific player; it answers "what would it take to beat the worst
    plausible competitor", not "will anyone else even bid".
    """
    if confidence not in ("low", "medium", "high"):
        raise HTTPException(
            status_code=400, detail="confidence must be one of: low, medium, high."
        )
    await players.ensure_fresh()
    lid = settings.league_id_for(league)
    fetched, users, rosters = await asyncio.gather(
        client.league(lid), client.users(lid), client.rosters(lid)
    )
    teams = services.build_teams(users, rosters)
    match = _match_team_or_404(teams, manager)

    total_budget = int((fetched.get("settings") or {}).get("waiver_budget") or 100)
    my_used = teams[match["roster_id"]]["record"].get("waiver_budget_used") or 0
    my_remaining = total_budget - my_used

    manager_data = await managers(league=league, seasons=None, days=None)
    others = [
        p for p in manager_data.get("managers") or [] if p["roster_id"] != match["roster_id"]
    ]
    for profile in others:
        used = teams.get(profile["roster_id"], {}).get("record", {}).get("waiver_budget_used") or 0
        profile["remaining_budget"] = total_budget - used

    serious_rivals = sorted(
        (
            p
            for p in others
            if p["remaining_budget"] >= 5 and (p.get("waivers") or {}).get("max_bid")
        ),
        key=lambda p: p["waivers"]["max_bid"],
        reverse=True,
    )
    top_rival = serious_rivals[0] if serious_rivals else None
    baseline = (
        top_rival["waivers"]["max_bid"]
        if top_rival
        else manager_data.get("league_context", {}).get("league_median_max_bid") or 5
    )
    margin = {"low": 1.05, "medium": 1.15, "high": 1.3}[confidence]
    recommended = min(round(baseline * margin), my_remaining) if my_remaining > 0 else 0

    return {
        "matched_on": manager,
        "player": players.resolve(player_id) if player_id else None,
        "your_remaining_budget": my_remaining,
        "recommended_bid": max(recommended, 0),
        "affordable": recommended <= my_remaining,
        "top_rival": (
            {
                "display_name": top_rival["display_name"],
                "team_name": top_rival["team_name"],
                "typical_bid": top_rival["waivers"]["typical_bid"],
                "max_bid_ever": top_rival["waivers"]["max_bid"],
                "remaining_budget": top_rival["remaining_budget"],
            }
            if top_rival
            else None
        ),
        "league_median_max_bid": manager_data.get("league_context", {}).get(
            "league_median_max_bid"
        ),
        "note": (
            "Based on this league's own bidding history from /managers, weighted "
            "toward whichever rival can both afford and has a habit of a high bid. "
            "No signal exists about who else actually wants this specific player."
        ),
    }


@app.get(
    "/leagues/{league}/briefing/{manager}",
    tags=["edge"],
    dependencies=[Depends(require_api_key)],
    summary="One weekly digest for your own roster",
)
async def weekly_briefing(
    league: str = Path(description="A slug from GET /leagues."),
    manager: str = Path(description="Username, display name or team name."),
    week: int | None = Query(default=None, ge=1, le=22),
) -> dict[str, Any]:
    """Merges what would otherwise be five separate calls into one weekly
    read on your own roster: injuries where ESPN and Sleeper disagree,
    byes coming up in the next two weeks, thin positions, the top trending
    free agents, and weather concerns for your players' games this week.

    Nothing here is computed specially for this endpoint - each block reuses
    the same logic as its own endpoint (/injury-report, /byes, /pressure,
    /available, /weather) and simply reports its own failure rather than
    failing the whole briefing, the same contract /snapshot's `include`
    blocks already use.
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
    roster_positions = fetched.get("roster_positions") or []
    resolved_team = services.resolve_roster(raw_roster, match, roster_positions, players)

    target_week = week or services.current_week(state)
    season = services._season_number(state, fetched)
    my_players = players.resolve_many([str(pid) for pid in (raw_roster.get("players") or [])])
    my_teams = {p["nfl_team"] for p in my_players if p.get("nfl_team")}

    async def _injury_disagreements() -> Any:
        checks = await asyncio.gather(
            *[public.get(f"/injury-report/{p['player_id']}") for p in my_players],
            return_exceptions=True,
        )
        disagreements = []
        for report in checks:
            if isinstance(report, BaseException) or not report.get("listed"):
                continue
            espn_status = (report.get("espn_report") or {}).get("status")
            sleeper_status = report.get("sleeper_injury_status")
            if espn_status and espn_status != sleeper_status:
                disagreements.append(report)
        return disagreements

    async def _upcoming_byes() -> Any:
        if not season:
            return {"error": "Current season could not be determined."}
        byes = (await public.get(f"/byes/{season}")).get("byes") or {}
        soon = [target_week + n for n in range(2)]
        return [
            {
                "name": p["name"],
                "position": p.get("position"),
                "nfl_team": p["nfl_team"],
                "bye_week": byes[p["nfl_team"]],
            }
            for p in my_players
            if p.get("nfl_team") and byes.get(p["nfl_team"]) in soon
        ]

    async def _weather_concerns() -> Any:
        weather = await public.get(f"/weather/{target_week}", params={"season": season})
        if not weather.get("available"):
            return {"error": weather.get("error", "Weather not available for this week.")}
        return [
            g
            for g in weather.get("outdoor_games_with_concerns") or []
            if g.get("home_team") in my_teams or g.get("away_team") in my_teams
        ]

    injury_disagreements, upcoming_byes, weather_concerns, available = await asyncio.gather(
        _guarded(_injury_disagreements()),
        _guarded(_upcoming_byes()),
        _guarded(_weather_concerns()),
        available_players(league=league, position=None, limit=3, season=season),
    )

    thin_positions = [
        b for b in positional_balance(resolved_team, roster_positions) if b["spare"] <= 0
    ]
    trending_free_agents = {
        position: [
            {"name": c["name"], "recent_average_points": c["recent_average_points"]}
            for c in candidates
        ]
        for position, candidates in (available.get("available") or {}).items()
        if candidates
    }

    return {
        "matched_on": manager,
        "week": target_week,
        "injury_disagreements": injury_disagreements,
        "upcoming_byes": upcoming_byes,
        "thin_positions": thin_positions,
        "trending_free_agents": trending_free_agents,
        "weather_concerns": weather_concerns,
    }


async def _guarded(coro: Any) -> Any:
    """Run one briefing block, reporting its own failure instead of failing
    the whole briefing - the same contract /snapshot's ?include= blocks use."""
    try:
        return await coro
    except HTTPException as exc:
        return {"error": str(exc.detail)}


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
