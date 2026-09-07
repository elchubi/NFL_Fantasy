"""MCP server wrapping the Sleeper fantasy league backend.

Exposes one tool per REST endpoint over Streamable HTTP, so the league can be
added to Claude.ai as a remote custom connector (Customize > Connectors > Add
custom connector).

Two things about how this is secured:

  * `BACKEND_API_KEY` lives here and is attached to every internal call. The
    Claude client never sees it.
  * The MCP server itself has no authentication of its own. It is protected by
    living at an unguessable URL - `https://<host>/<long-random-token>/mcp`.
    That is adequate for a single-user server and nothing more; sharing this
    with anyone else means moving to OAuth 2.1 (see the README).

Tool descriptions matter more than usual here: they are what the model reads to
decide whether to call something, so each one says when to reach for it rather
than restating its name.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse

from backend import Backend, BackendError

VERSION = "1.0.0"

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sleeper-mcp")

backend = Backend()

mcp = MCPServer(
    name="sleeper-fantasy-league",
    title="Sleeper Fantasy League",
    version=VERSION,
    instructions=(
        "Read-only access to one or more Sleeper fantasy football leagues, with "
        "player IDs already resolved to names, plus advanced usage stats, "
        "betting lines, injury reports with practice participation, stadium "
        "weather, a rookie draft board, opponent profiling and a decision "
        "log.\n\n"
        "This backend can serve several leagues at once. Every league_*, "
        "manager_*, decision_* and history_* tool (except history_capture, "
        "which is shared) takes a `league` argument - call league_list once to "
        "see what is available. If the server has DEFAULT_LEAGUE configured, "
        "`league` can be omitted and that one is used automatically; a tool "
        "call that omits it without a default set fails with a message saying "
        "so, which is the cue to call league_list.\n\n"
        "Start with league_snapshot for the current state of the league. Use "
        "manager_pressure and manager_list when the question is about trading "
        "with or bidding against another manager - those are specific to this "
        "league and are not available anywhere else.\n\n"
        "manager_list and manager_profile report a `source` field: 'archive' means "
        "they read multi-season history, 'live (current season only)' means "
        "history_backfill has never been run. If the user asks anything that "
        "would benefit from past seasons (career FAAB behaviour, draft tendencies "
        "over time, how a manager has trended) and source is 'live', call "
        "history_backfill once yourself before answering, then retry the read - "
        "no need to ask permission first, it only reads from Sleeper and archives "
        "to this league's own database."
    ),
)

def _resolve_league(league: str | None) -> str:
    """The league slug to use for this call.

    This backend can serve several leagues at once, so every per-league tool
    takes a `league` argument. An explicit one always wins. Otherwise this
    falls back to `DEFAULT_LEAGUE` (set on the MCP server when it is only ever
    pointed at one league day to day); with neither set, it tells the model to
    call `league_list` and choose rather than silently guessing and answering
    about the wrong league.
    """
    if league and league.strip():
        return league.strip()
    default = os.getenv("DEFAULT_LEAGUE", "").strip()
    if default:
        return default
    raise ToolError(
        "No `league` given and DEFAULT_LEAGUE is not configured on this MCP "
        "server. Call league_list to see which leagues this backend serves, "
        "then pass one as `league`."
    )


async def _get(path: str, **kwargs: Any) -> Any:
    """GET the backend, surfacing its error message to the model.

    The backend writes errors to be acted on - "no gsis_id for this player, so
    there is nothing to join", "matches more than one team, here are the
    candidates". Letting the exception propagate as-is loses that text: the SDK
    reports a bare "Error executing tool <name>" and logs a crash. Raising
    ToolError puts the message in front of the model, which is usually enough
    for it to fix the call itself.
    """
    try:
        return await backend.get(path, **kwargs)
    except BackendError as exc:
        raise ToolError(str(exc)) from exc


async def _post(path: str, **kwargs: Any) -> Any:
    try:
        return await backend.post(path, **kwargs)
    except BackendError as exc:
        raise ToolError(str(exc)) from exc


# Read-only tools declare it so a client knows they never move state.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
# Append-only writes: they add a row and can never edit or delete one.
APPEND_ONLY = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, open_world_hint=True
)
# Writes that can safely be repeated: the backend skips rows identical to the
# last recorded state, so calling twice records nothing the second time.
IDEMPOTENT_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)


# --- League (Sleeper) ---------------------------------------------------------


@mcp.tool(
    name="league_list",
    annotations=READ_ONLY,
    description=(
        "The leagues this backend serves. This backend can host several leagues "
        "at once, each with its own slug; every other league_*, manager_*, "
        "decision_* and history_* tool takes that slug as `league`. Call this "
        "first if `league` is unknown, or if a tool call fails saying no league "
        "was given."
    ),
)
async def league_list() -> Any:
    return await _get("/leagues")


@mcp.tool(
    name="league_snapshot",
    annotations=READ_ONLY,
    description=(
        "The whole league in one call: every team with its manager, full rosters "
        "split into starters by lineup slot, bench and IR, standings, the week's "
        "matchups and recent transactions - all with player names, positions, NFL "
        "teams and injury status already resolved. Start here for almost any "
        "question about the league. Pass `include` to attach external sources."
    ),
)
async def league_snapshot(
    league: str | None = None,
    week: int | None = None,
    days: int = 7,
    include: str | None = None,
) -> Any:
    """
    Args:
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
        week: NFL week for matchups (1-22). Defaults to the current week.
        days: How many days of transactions to include (1-120).
        include: Comma-separated extras to attach, any of: advanced_stats, odds,
            injury_report, weather. Omit to keep the response small - each one
            costs an upstream fetch.
    """
    lid = _resolve_league(league)
    return await _get(
        f"/leagues/{lid}/snapshot", params={"week": week, "days": days, "include": include}
    )


@mcp.tool(
    name="league_settings",
    annotations=READ_ONLY,
    description=(
        "League rules in plain language: scoring (including whether it is PPR), "
        "the starting lineup slots, playoff format, trade deadline and waiver "
        "rules. Use it when a decision depends on how the league is configured - "
        "how much a reception is worth, how much FAAB budget exists, whether "
        "trades are still allowed."
    ),
)
async def league_settings(league: str | None = None) -> Any:
    """
    Args:
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
    """
    lid = _resolve_league(league)
    return await _get(f"/leagues/{lid}/league-settings")


@mcp.tool(
    name="league_roster",
    annotations=READ_ONLY,
    description=(
        "One team's roster, resolved, without pulling the whole league. Search is "
        "flexible and case-insensitive across username, display name and team name, "
        "trying exact match first, then prefix, then substring - 'tacos' finds "
        "'Los Tacos Voladores'. If the search matches several teams it returns a "
        "409 listing the candidates; if it matches none, a 404 listing every team."
    ),
)
async def league_roster(manager: str, league: str | None = None) -> Any:
    """
    Args:
        manager: Username, display name or team name. Partial matches are fine.
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
    """
    lid = _resolve_league(league)
    return await _get(f"/leagues/{lid}/roster/{manager}")


@mcp.tool(
    name="waivers_available",
    annotations=READ_ONLY,
    description=(
        "Every free agent in this league - nobody's roster - ranked by recent role "
        "trend and points scored under this league's own scoring settings, not a "
        "generic PPR ranking. This is the actual waiver-wire question ('who do I "
        "add') answered against who is really still out there in this league, not "
        "a public top-100 list that ignores who your league has already rostered."
    ),
)
async def waivers_available(
    league: str | None = None,
    position: str | None = None,
    limit: int = 25,
    season: int | None = None,
) -> Any:
    """
    Args:
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
        position: QB, RB, WR or TE. Omit to check all four.
        limit: Top N per position (1-100).
        season: Defaults to the current season.
    """
    lid = _resolve_league(league)
    return await _get(
        f"/leagues/{lid}/available",
        params={"position": position, "limit": limit, "season": season},
    )


@mcp.tool(
    name="schedule_difficulty",
    annotations=READ_ONLY,
    description=(
        "For each of a roster's QB/RB/WR/TE, how many fantasy points its next few "
        "opponents have allowed at that position this season, under this league's "
        "own scoring rules - not the opponent's real-world defensive rank. Use it "
        "to break a close start/sit or trade-value call between two similar "
        "players: the one with the softer slate ahead is worth more right now."
    ),
)
async def schedule_difficulty(
    manager: str, league: str | None = None, weeks_ahead: int = 4, season: int | None = None
) -> Any:
    """
    Args:
        manager: Username, display name or team name. Partial matches are fine.
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
        weeks_ahead: How many upcoming weeks to check (1-10).
        season: Defaults to the current season.
    """
    lid = _resolve_league(league)
    return await _get(
        f"/leagues/{lid}/schedule-difficulty/{manager}",
        params={"weeks_ahead": weeks_ahead, "season": season},
    )


# --- External sources ---------------------------------------------------------


@mcp.tool(
    name="stats_advanced",
    annotations=READ_ONLY,
    description=(
        "Advanced usage for one player from nflverse: snap share, target share, "
        "air yards, red zone touches and EPA. Returns a season average, a "
        "last-three-week average and the delta between them, which is the earliest "
        "signal that a player's role is changing. Use it when the question is "
        "whether someone is gaining or losing work, not just how many points they "
        "scored. Team defenses have no data here."
    ),
)
async def stats_advanced(player_id: str, season: int | None = None) -> Any:
    """
    Args:
        player_id: Sleeper player id, as it appears in league_snapshot rosters.
        season: Season to pull. Defaults to the current NFL season.
    """
    return await _get(f"/advanced-stats/{player_id}", params={"season": season})


@mcp.tool(
    name="stats_odds",
    annotations=READ_ONLY,
    description=(
        "Betting lines for a week: spread, total, moneyline, who is favoured and "
        "the implied team totals. The best public proxy for game script - big "
        "favourites run the ball late, big underdogs throw - so use it when "
        "deciding between players in very different game environments."
    ),
)
async def stats_odds(week: int, season: int | None = None) -> Any:
    """
    Args:
        week: NFL week (1-22).
        season: Season. Defaults to the current one.
    """
    return await _get(f"/odds/{week}", params={"season": season})


@mcp.tool(
    name="stats_injury_report_team",
    annotations=READ_ONLY,
    description=(
        "Every listed injury for one NFL team from ESPN, including practice "
        "participation (full, limited or did not practice) when ESPN publishes it. "
        "Practice participation is often fresher and more telling than a game "
        "status. Use it to check a whole team at once."
    ),
)
async def stats_injury_report_team(team: str) -> Any:
    """
    Args:
        team: NFL team abbreviation, e.g. KC, SF, NYJ.
    """
    return await _get("/injury-report", params={"team": team})


@mcp.tool(
    name="stats_injury_report_player",
    annotations=READ_ONLY,
    description=(
        "One player's injury detail from ESPN, shown next to the status Sleeper "
        "has cached, so you can see when the two disagree - ESPN is usually the "
        "fresher of the two. Use it before a start/sit call on anyone questionable."
    ),
)
async def stats_injury_report_player(player_id: str) -> Any:
    """
    Args:
        player_id: Sleeper player id.
    """
    return await _get(f"/injury-report/{player_id}")


@mcp.tool(
    name="stats_weather",
    annotations=READ_ONLY,
    description=(
        "Kickoff weather at each of the week's stadiums, with a read on whether it "
        "actually matters. Only relevant for kickers and the deep passing game; "
        "domes return indoor=true with no forecast at all. Use it when wind or "
        "snow could swing a close start/sit decision."
    ),
)
async def stats_weather(week: int, season: int | None = None) -> Any:
    """
    Args:
        week: NFL week (1-22).
        season: Season. Defaults to the current one.
    """
    return await _get(f"/weather/{week}", params={"season": season})


@mcp.tool(
    name="stats_stadiums",
    annotations=READ_ONLY,
    description=(
        "The static stadium reference: coordinates and roof type for all 32 teams. "
        "Mostly useful for checking which venues are indoors before reasoning about "
        "weather."
    ),
)
async def stats_stadiums() -> Any:
    return await _get("/stadiums")


# --- League-specific edge -----------------------------------------------------


@mcp.tool(
    name="manager_list",
    annotations=READ_ONLY,
    description=(
        "Behavioural profile of every manager in the league, built from this "
        "league's own transaction and draft history: FAAB habits (typical bid, "
        "highest ever, win rate on contested claims), which day they make moves, "
        "how active they are, draft tendencies by position, and who they have "
        "traded with. Use it before bidding or opening a trade - knowing someone "
        "has never bid above $12 is worth more than any projection."
    ),
)
async def manager_list(
    league: str | None = None, seasons: str | None = None, days: int | None = None
) -> Any:
    """
    Args:
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
        seasons: Comma-separated seasons, e.g. "2025,2026". Defaults to every
            season archived by history_backfill.
        days: Only count transactions from the last N days. Omit to use the
            whole archive, which is what makes the profile multi-season.
    """
    lid = _resolve_league(league)
    return await _get(f"/leagues/{lid}/managers", params={"seasons": seasons, "days": days})


@mcp.tool(
    name="manager_profile",
    annotations=READ_ONLY,
    description=(
        "One manager's profile read against the rest of the league. Use it when "
        "dealing with a specific opponent rather than surveying everyone."
    ),
)
async def manager_profile(
    name: str, league: str | None = None, seasons: str | None = None, days: int | None = None
) -> Any:
    """
    Args:
        name: Username, display name or team name.
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
        seasons: Comma-separated seasons. Defaults to everything archived.
        days: Only count transactions from the last N days.
    """
    lid = _resolve_league(league)
    return await _get(f"/leagues/{lid}/manager/{name}", params={"seasons": seasons, "days": days})


@mcp.tool(
    name="manager_pressure",
    annotations=READ_ONLY,
    description=(
        "Which teams are structurally forced to make a move, ranked by urgency: "
        "colliding bye weeks, stacked injuries, and positions with no healthy "
        "cover. A manager who has to act before you do is one you have leverage "
        "over, so check this before proposing a trade."
    ),
)
async def manager_pressure(
    league: str | None = None, week: int | None = None, horizon: int = 3
) -> Any:
    """
    Args:
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
        week: Week to analyse from. Defaults to the current week.
        horizon: How many weeks ahead to look (1-6).
    """
    lid = _resolve_league(league)
    return await _get(f"/leagues/{lid}/pressure", params={"week": week, "horizon": horizon})


@mcp.tool(
    name="manager_playoff_odds",
    annotations=READ_ONLY,
    description=(
        "Monte Carlo playoff odds for every team, simulated from each team's own "
        "scoring history (not a projection system), plus a buyer/bubble/seller "
        "read per team. Cross this with manager_pressure or a roster before "
        "proposing a trade: a team already locked into the playoffs is a soft "
        "target for a win-now player; a team with long odds should be selling "
        "its expiring value, whether it has noticed or not."
    ),
)
async def manager_playoff_odds(league: str | None = None, trials: int = 3000) -> Any:
    """
    Args:
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
        trials: Simulated seasons to run (100-20000). Higher is slower but
            less noisy; the default is plenty for a single read.
    """
    lid = _resolve_league(league)
    return await _get(f"/leagues/{lid}/playoff-odds", params={"trials": trials})


@mcp.tool(
    name="manager_trade_fits",
    annotations=READ_ONLY,
    description=(
        "Who to approach for a trade, and about what position: crosses your "
        "thin positions (no spare healthy body beyond your starters) against "
        "every other team's surplus at that position, weighted by their "
        "playoff odds (sellers ranked first) and their trade history with you. "
        "Use this once you know what you need - manager_pressure or a roster "
        "read tells you that - to shortlist who actually has it to give."
    ),
)
async def manager_trade_fits(manager: str, league: str | None = None) -> Any:
    """
    Args:
        manager: Username, display name or team name - your own team.
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
    """
    lid = _resolve_league(league)
    return await _get(f"/leagues/{lid}/trade-fits/{manager}")


@mcp.tool(
    name="decision_log",
    annotations=APPEND_ONLY,
    description=(
        "Record a decision and the reasoning behind it, at the moment it is made "
        "and before the outcome is known. Append-only: it adds an entry and can "
        "never edit or delete one. Returns a decision_id to pass to "
        "decision_log_outcome later. Worth doing for any call the user might want "
        "to revisit - contested waiver bids, close start/sit calls, trades."
    ),
)
async def decision_log(
    kind: Literal[
        "waiver_bid", "trade", "start_sit", "draft_pick", "keeper", "drop", "other"
    ],
    summary: str,
    league: str | None = None,
    reasoning: str | None = None,
    players_involved: str | None = None,
    confidence: str | None = None,
    expected: str | None = None,
    week: int | None = None,
    season: int | None = None,
) -> Any:
    """
    Args:
        kind: What sort of decision this is.
        summary: One line saying what was decided.
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
        reasoning: Why. This is the part worth having later.
        players_involved: Comma-separated player names.
        confidence: How sure the user was, e.g. low, medium, high.
        expected: What they expect to happen.
        week: Defaults to the current week.
        season: Defaults to the current season.
    """
    lid = _resolve_league(league)
    return await _post(
        f"/leagues/{lid}/decision",
        params={
            "kind": kind,
            "summary": summary,
            "reasoning": reasoning,
            "players_involved": players_involved,
            "confidence": confidence,
            "expected": expected,
            "week": week,
            "season": season,
        },
    )


@mcp.tool(
    name="decision_log_outcome",
    annotations=APPEND_ONLY,
    description=(
        "Record how a previously logged decision turned out. Append-only: the "
        "outcome is layered on top and the original call and reasoning are never "
        "edited, which is the whole point when reviewing them later."
    ),
)
async def decision_log_outcome(
    decision_id: str, outcome: str, league: str | None = None, season: int | None = None
) -> Any:
    """
    Args:
        decision_id: The id returned by decision_log.
        outcome: What actually happened.
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
        season: Season the decision was logged in. Defaults to the current one.
    """
    lid = _resolve_league(league)
    return await _post(
        f"/leagues/{lid}/decision/{decision_id}/outcome",
        params={"outcome": outcome, "season": season},
    )


@mcp.tool(
    name="decision_list",
    annotations=READ_ONLY,
    description=(
        "Read back the decision log with outcomes attached. Use it to review past "
        "calls, to find decisions still awaiting an outcome (pending_only=true), "
        "or to look for patterns in where the user's judgement has been off."
    ),
)
async def decision_list(
    league: str | None = None,
    season: int | None = None,
    week: int | None = None,
    kind: str | None = None,
    pending_only: bool = False,
) -> Any:
    """
    Args:
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
        season: Defaults to the current season.
        week: Filter to one week.
        kind: Filter by decision kind.
        pending_only: Only decisions with no outcome recorded yet.
    """
    lid = _resolve_league(league)
    return await _get(
        f"/leagues/{lid}/decisions",
        params={"season": season, "week": week, "kind": kind, "pending_only": pending_only},
    )


# --- Rookie draft -------------------------------------------------------------


@mcp.tool(
    name="draft_class",
    annotations=READ_ONLY,
    description=(
        "The rookie draft board for a class: every skill-position pick with its "
        "draft capital, age, combine measurables, and landing spot - how much of "
        "the workload at that position actually vacated on the team that drafted "
        "him. Use it for draft prep and for judging rookies during the season."
    ),
)
async def draft_class(
    season: int,
    position: str | None = None,
    round_max: int | None = None,
    landing: bool = True,
) -> Any:
    """
    Args:
        season: Draft year, e.g. 2026.
        position: Filter to QB, RB, WR or TE.
        round_max: Only picks from this round or earlier.
        landing: Compute the landing spot. Set false to skip that work.
    """
    return await _get(
        f"/draft-class/{season}",
        params={"position": position, "round_max": round_max, "landing": landing},
    )


@mcp.tool(
    name="draft_prospect",
    annotations=READ_ONLY,
    description=(
        "One prospect's draft profile: capital, age, combine numbers and landing "
        "spot. Accepts either a Sleeper player id or an nflverse gsis_id, so it "
        "works straight from a roster or from advanced stats."
    ),
)
async def draft_prospect(player_id: str, season: int | None = None) -> Any:
    """
    Args:
        player_id: Sleeper player id, or a gsis_id like 00-0041027.
        season: Draft year. Looked up automatically if omitted.
    """
    return await _get(f"/prospect/{player_id}", params={"season": season})


# --- History ------------------------------------------------------------------


@mcp.tool(
    name="history_capture",
    annotations=IDEMPOTENT_WRITE,
    description=(
        "Archive this week's betting lines and injury reports, which are the only "
        "two sources that cannot be fetched again once the week has passed. Safe "
        "to call repeatedly: rows identical to the last recorded state are skipped, "
        "so a second call in a row records nothing. Normally this runs on a "
        "schedule; call it manually to capture a specific moment."
    ),
)
async def history_capture(
    week: int | None = None,
    season: int | None = None,
    teams: str | None = None,
    refresh: bool = True,
) -> Any:
    """
    Args:
        week: Defaults to the current week.
        season: Defaults to the current season.
        teams: Comma-separated team abbreviations. Defaults to all 32.
        refresh: Bypass the read caches so the archived line is the live one.
    """
    return await _post(
        "/capture",
        params={"week": week, "season": season, "teams": teams, "refresh": refresh},
    )


@mcp.tool(
    name="history_backfill",
    annotations=IDEMPOTENT_WRITE,
    description=(
        "Walk the league's season chain and archive every season's transactions, "
        "draft picks and managers. In Sleeper each season is a separate league, so "
        "this is the only way to reach past ones - manager profiling is limited to "
        "the current season until this has run. Safe to repeat: finished seasons "
        "are skipped, only the season in progress is re-read. Run it once after "
        "deploying and again when a season ends."
    ),
)
async def history_backfill(
    league: str | None = None, refresh: bool = False, limit: int = 20
) -> Any:
    """
    Args:
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
        refresh: Re-read seasons already archived. A finished season cannot
            change, so this is only useful after a bug fix.
        limit: How many seasons back to walk.
    """
    lid = _resolve_league(league)
    return await _post(f"/leagues/{lid}/backfill", params={"refresh": refresh, "limit": limit})


@mcp.tool(
    name="history_seasons",
    annotations=READ_ONLY,
    description=(
        "The league's seasons, newest first. Use it to see which seasons are "
        "available to profile over, or with discover=true to check what a backfill "
        "would pick up before running one."
    ),
)
async def history_seasons(league: str | None = None, discover: bool = False) -> Any:
    """
    Args:
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
        discover: Follow previous_league_id against Sleeper instead of reading
            what is already archived.
    """
    lid = _resolve_league(league)
    return await _get(f"/leagues/{lid}/seasons", params={"discover": discover})


@mcp.tool(
    name="history_inventory",
    annotations=READ_ONLY,
    description=(
        "What the archive holds: rows, weeks and file size per source and season. "
        "Use it to check whether there is enough history for a question before "
        "trying to answer it from the archive."
    ),
)
async def history_inventory(league: str | None = None) -> Any:
    """
    Args:
        league: A slug from league_list. Defaults to DEFAULT_LEAGUE when this
            server is only ever pointed at one league.
    """
    lid = _resolve_league(league)
    return await _get(f"/leagues/{lid}/history")


@mcp.tool(
    name="history_source",
    annotations=READ_ONLY,
    description=(
        "Read archived rows for one source, oldest first. A subject reappears only "
        "when it actually changed, so the sequence is the history - a line that "
        "moved, or a player who went from limited to full practice."
    ),
)
async def history_source(
    source: Literal["odds", "injuries", "decisions"],
    league: str | None = None,
    season: int | None = None,
    week: int | None = None,
    limit: int | None = None,
) -> Any:
    """
    Args:
        source: Which archive to read. "odds" and "injuries" are shared across
            every league; "decisions" is one league's own.
        league: A slug from league_list. The backend routes every source
            through a league even though "odds" and "injuries" do not actually
            use it. Defaults to DEFAULT_LEAGUE when this server is only ever
            pointed at one league.
        season: Defaults to the current season.
        week: Filter to one week.
        limit: Return only the most recent N rows.
    """
    lid = _resolve_league(league)
    return await _get(
        f"/leagues/{lid}/history/{source}",
        params={"season": season, "week": week, "limit": limit},
    )


# --- Health -------------------------------------------------------------------


@mcp.tool(
    name="health_check",
    annotations=READ_ONLY,
    description=(
        "Check that the league backend is up and see how fresh its caches are. "
        "Use it when other tools are failing, to tell a backend problem from a "
        "question the data cannot answer."
    ),
)
async def health_check() -> Any:
    # /health takes no API key, and asking for one would hide a misconfigured
    # key behind an error from the one endpoint meant to diagnose it.
    return await _get("/health", authenticated=False)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Any) -> JSONResponse:
    """Liveness probe for the MCP server itself, separate from the backend's."""
    return JSONResponse(
        {
            "status": "ok",
            "service": "sleeper-fantasy-mcp",
            "version": VERSION,
            "backend_url": backend.base_url,
            "backend_api_key_configured": bool(backend.api_key),
            "tools": len(TOOL_NAMES),
        }
    )


TOOL_NAMES = [
    "league_list",
    "league_snapshot", "league_settings", "league_roster", "waivers_available",
    "schedule_difficulty",
    "stats_advanced", "stats_odds", "stats_injury_report_team",
    "stats_injury_report_player", "stats_weather", "stats_stadiums",
    "manager_list", "manager_profile", "manager_pressure", "manager_playoff_odds",
    "manager_trade_fits",
    "decision_log", "decision_log_outcome", "decision_list",
    "draft_class", "draft_prospect",
    "history_backfill", "history_seasons",
    "history_capture", "history_inventory", "history_source",
    "health_check",
]


# --- Wiring -------------------------------------------------------------------


def mcp_path() -> str:
    """The URL path the connector is served at, including the secret segment."""
    token = os.getenv("MCP_URL_TOKEN", "").strip("/")
    return f"/{token}/mcp" if token else "/mcp"


def transport_security() -> TransportSecuritySettings | None:
    """Host allow-list for the Streamable HTTP transport.

    The SDK enables DNS-rebinding protection by default and matches Host headers
    exactly - there is no `*` wildcard - so a public deployment answers 421 to
    every request until the real hostname is listed. Set MCP_ALLOWED_HOSTS to
    your domain. Left unset, protection is turned off so the container is usable
    out of the box, with a warning; the URL token is what actually guards it.
    """
    raw = os.getenv("MCP_ALLOWED_HOSTS", "").strip()
    if not raw:
        log.warning(
            "MCP_ALLOWED_HOSTS is not set; DNS rebinding protection is off. "
            "Set it to your public hostname (e.g. mcp.example.com) in production."
        )
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    hosts: list[str] = []
    for entry in raw.split(","):
        host = entry.strip()
        if not host:
            continue
        hosts.append(host)
        # Accept the same host on any port, which is how it arrives behind some
        # reverse proxies.
        if ":" not in host:
            hosts.append(f"{host}:*")

    origins = [f"https://{h}" for h in hosts if not h.endswith(":*")]
    origins += [f"http://{h}" for h in hosts if not h.endswith(":*")]
    log.info("DNS rebinding protection on; allowed hosts: %s", ", ".join(hosts))
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


def build_app() -> Any:
    path = mcp_path()
    if path == "/mcp":
        log.warning(
            "MCP_URL_TOKEN is not set, so the server is at a guessable path. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    log.info("Serving MCP over Streamable HTTP at %s (%s tools)", path, len(TOOL_NAMES))
    return mcp.streamable_http_app(
        streamable_http_path=path,
        # Stateless keeps every request self-contained, which survives restarts
        # and more than one worker without losing a session mid-conversation.
        stateless_http=True,
        transport_security=transport_security(),
    )


app = build_app()


if __name__ == "__main__":  # pragma: no cover - local convenience
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
    )
