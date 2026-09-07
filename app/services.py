"""Turn raw Sleeper payloads into resolved, human-readable structures."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from app import humanize
from app.players import PlayerStore
from app.sleeper import SleeperClient

TRANSACTION_TYPE_LABELS = {
    "free_agent": "free_agent",
    "waiver": "waiver",
    "trade": "trade",
    "commissioner": "commissioner",
}


def _epoch_ms_to_iso(value: Any) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def current_week(state: dict[str, Any]) -> int:
    """The week Sleeper considers current, clamped to something fetchable."""
    for key in ("display_week", "week", "leg"):
        value = state.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 1


# --- Teams -------------------------------------------------------------------


def build_teams(
    users: Iterable[dict[str, Any]],
    rosters: Iterable[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Map roster_id -> team identity (manager + team name + record)."""
    users_by_id = {u.get("user_id"): u for u in users}
    teams: dict[int, dict[str, Any]] = {}

    for roster in rosters:
        roster_id = roster.get("roster_id")
        owner = users_by_id.get(roster.get("owner_id")) or {}
        metadata = owner.get("metadata") or {}
        settings = roster.get("settings") or {}

        co_owners = [
            (users_by_id.get(uid) or {}).get("display_name")
            for uid in (roster.get("co_owners") or [])
        ]

        teams[roster_id] = {
            "roster_id": roster_id,
            "owner_id": roster.get("owner_id"),
            "display_name": owner.get("display_name"),
            "username": owner.get("username") or owner.get("display_name"),
            "team_name": metadata.get("team_name") or owner.get("display_name"),
            "co_owners": [name for name in co_owners if name],
            "record": {
                "wins": settings.get("wins", 0),
                "losses": settings.get("losses", 0),
                "ties": settings.get("ties", 0),
                "points_for": _points(settings, "fpts"),
                "points_against": _points(settings, "fpts_against"),
                "waiver_position": settings.get("waiver_position"),
                "waiver_budget_used": settings.get("waiver_budget_used"),
                "total_moves": settings.get("total_moves"),
            },
        }
    return teams


def _points(settings: dict[str, Any], key: str) -> float:
    whole = settings.get(key) or 0
    decimal = settings.get(f"{key}_decimal") or 0
    return round(float(whole) + float(decimal) / 100, 2)


def team_label(teams: dict[int, dict[str, Any]], roster_id: Any) -> dict[str, Any]:
    team = teams.get(roster_id)
    if not team:
        return {"roster_id": roster_id, "display_name": None, "team_name": None}
    return {
        "roster_id": roster_id,
        "display_name": team["display_name"],
        "team_name": team["team_name"],
    }


# --- Rosters -----------------------------------------------------------------


def resolve_roster(
    roster: dict[str, Any],
    team: dict[str, Any],
    roster_positions: list[str],
    players: PlayerStore,
) -> dict[str, Any]:
    """Split a roster into starters (by lineup slot), bench, IR and taxi."""
    starter_ids = [pid for pid in (roster.get("starters") or [])]
    slots = [p for p in roster_positions if p not in humanize.NON_STARTING_SLOTS]

    starters: list[dict[str, Any]] = []
    for index, slot in enumerate(slots):
        player_id = starter_ids[index] if index < len(starter_ids) else None
        entry: dict[str, Any] = {
            "slot": slot,
            "slot_label": humanize.slot_label(slot),
        }
        if player_id in (None, "0", 0, ""):
            entry["player"] = None
            entry["empty"] = True
        else:
            entry["player"] = players.resolve(player_id)
        starters.append(entry)

    # Anything Sleeper listed as a starter beyond the known slots (shouldn't
    # happen, but never silently drop a player).
    for extra_id in starter_ids[len(slots) :]:
        if extra_id in (None, "0", 0, ""):
            continue
        starters.append(
            {
                "slot": "UNKNOWN",
                "slot_label": "Unmapped lineup slot",
                "player": players.resolve(extra_id),
            }
        )

    reserve_ids = [str(p) for p in (roster.get("reserve") or [])]
    taxi_ids = [str(p) for p in (roster.get("taxi") or [])]
    started = {str(p) for p in starter_ids if p not in (None, "0", 0, "")}
    all_ids = [str(p) for p in (roster.get("players") or [])]

    bench_ids = [
        pid
        for pid in all_ids
        if pid not in started and pid not in reserve_ids and pid not in taxi_ids
    ]

    return {
        **team,
        "starters": starters,
        "bench": players.resolve_many(bench_ids),
        "injured_reserve": players.resolve_many(reserve_ids),
        "taxi_squad": players.resolve_many(taxi_ids),
        "player_count": len(all_ids),
    }


def find_team(
    teams: dict[int, dict[str, Any]],
    query: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Flexible, case-insensitive lookup by username, display name or team name.

    Returns (match, candidates). When the query is ambiguous, match is None and
    candidates holds everything that matched.
    """
    needle = " ".join(query.strip().lower().split())
    if not needle:
        return None, []

    def haystacks(team: dict[str, Any]) -> list[str]:
        values = [team.get("display_name"), team.get("username"), team.get("team_name")]
        values.extend(team.get("co_owners") or [])
        return [" ".join(str(v).lower().split()) for v in values if v]

    exact = [t for t in teams.values() if needle in haystacks(t)]
    if len(exact) == 1:
        return exact[0], exact
    if len(exact) > 1:
        return None, exact

    prefix = [t for t in teams.values() if any(h.startswith(needle) for h in haystacks(t))]
    if len(prefix) == 1:
        return prefix[0], prefix
    if len(prefix) > 1:
        return None, prefix

    partial = [t for t in teams.values() if any(needle in h for h in haystacks(t))]
    if len(partial) == 1:
        return partial[0], partial
    return None, partial


# --- Matchups ----------------------------------------------------------------


def build_matchups(
    raw_matchups: list[dict[str, Any]],
    teams: dict[int, dict[str, Any]],
    roster_positions: list[str],
    players: PlayerStore,
) -> list[dict[str, Any]]:
    """Group matchup rows into head-to-head pairings with resolved lineups."""
    slots = [p for p in roster_positions if p not in humanize.NON_STARTING_SLOTS]
    grouped: dict[Any, list[dict[str, Any]]] = {}

    for row in raw_matchups:
        matchup_id = row.get("matchup_id")
        starter_ids = row.get("starters") or []
        starter_points = row.get("starters_points") or []
        player_points = row.get("players_points") or {}

        lineup = []
        for index, player_id in enumerate(starter_ids):
            slot = slots[index] if index < len(slots) else "UNKNOWN"
            points = (
                starter_points[index]
                if index < len(starter_points)
                else player_points.get(str(player_id))
            )
            lineup.append(
                {
                    "slot": slot,
                    "slot_label": humanize.slot_label(slot),
                    "player": (
                        players.resolve(player_id)
                        if player_id not in (None, "0", 0, "")
                        else None
                    ),
                    "points": points,
                }
            )

        entry = {
            **team_label(teams, row.get("roster_id")),
            "points": row.get("points"),
            "starters": lineup,
        }
        grouped.setdefault(matchup_id, []).append(entry)

    matchups: list[dict[str, Any]] = []
    for matchup_id, entries in sorted(
        grouped.items(), key=lambda kv: (kv[0] is None, kv[0])
    ):
        if matchup_id is None:
            # Teams on a bye (or a league without scheduled matchups yet).
            for entry in entries:
                matchups.append(
                    {"matchup_id": None, "teams": [entry], "bye": True, "leader": None}
                )
            continue

        leader = None
        if len(entries) == 2:
            a, b = entries
            pa, pb = a.get("points") or 0, b.get("points") or 0
            if pa != pb:
                winning = a if pa > pb else b
                leader = {
                    "team_name": winning.get("team_name"),
                    "display_name": winning.get("display_name"),
                    "margin": round(abs(pa - pb), 2),
                }
        matchups.append(
            {
                "matchup_id": matchup_id,
                "teams": entries,
                "bye": False,
                "leader": leader,
            }
        )
    return matchups


# --- Transactions ------------------------------------------------------------


def _weeks_to_scan(week: int, days: int) -> list[int]:
    """Weeks worth fetching to cover the last `days` days of activity."""
    span = int(math.ceil(days / 7)) + 1
    first = max(1, week - span)
    return list(range(week, first - 1, -1))


def build_transactions(
    raw: Iterable[dict[str, Any]],
    teams: dict[int, dict[str, Any]],
    players: PlayerStore,
    users_by_id: dict[str, dict[str, Any]],
    since_epoch_ms: float,
) -> list[dict[str, Any]]:
    """Resolve transactions and keep only those newer than `since_epoch_ms`."""
    resolved: list[dict[str, Any]] = []

    for tx in raw:
        stamp = tx.get("status_updated") or tx.get("created")
        if stamp and float(stamp) < since_epoch_ms:
            continue

        adds = tx.get("adds") or {}
        drops = tx.get("drops") or {}
        creator = users_by_id.get(tx.get("creator")) or {}
        settings = tx.get("settings") or {}

        entry: dict[str, Any] = {
            "transaction_id": tx.get("transaction_id"),
            "type": TRANSACTION_TYPE_LABELS.get(tx.get("type"), tx.get("type")),
            "status": tx.get("status"),
            "week": tx.get("leg"),
            "created": _epoch_ms_to_iso(tx.get("created")),
            "updated": _epoch_ms_to_iso(tx.get("status_updated")),
            "made_by": {
                "display_name": creator.get("display_name"),
                "team_name": (creator.get("metadata") or {}).get("team_name"),
            },
            "teams_involved": [
                team_label(teams, rid) for rid in (tx.get("roster_ids") or [])
            ],
            "adds": [
                {"player": players.resolve(pid), "to_team": team_label(teams, rid)}
                for pid, rid in adds.items()
            ],
            "drops": [
                {"player": players.resolve(pid), "from_team": team_label(teams, rid)}
                for pid, rid in drops.items()
            ],
        }

        if settings.get("waiver_bid") is not None:
            entry["waiver_bid"] = settings["waiver_bid"]
        if tx.get("waiver_budget"):
            entry["faab_transfers"] = [
                {
                    "from_team": team_label(teams, item.get("sender")),
                    "to_team": team_label(teams, item.get("receiver")),
                    "amount": item.get("amount"),
                }
                for item in tx["waiver_budget"]
            ]
        if tx.get("draft_picks"):
            entry["draft_picks"] = [
                {
                    "season": pick.get("season"),
                    "round": pick.get("round"),
                    "original_team": team_label(teams, pick.get("roster_id")),
                    "from_team": team_label(teams, pick.get("previous_owner_id")),
                    "to_team": team_label(teams, pick.get("owner_id")),
                }
                for pick in tx["draft_picks"]
            ]
        notes = (tx.get("metadata") or {}).get("notes")
        if notes:
            entry["notes"] = notes

        entry["summary"] = _transaction_summary(entry)
        resolved.append(entry)

    resolved.sort(key=lambda t: t.get("updated") or t.get("created") or "", reverse=True)
    return resolved


def _transaction_summary(entry: dict[str, Any]) -> str:
    who = entry["made_by"].get("team_name") or entry["made_by"].get("display_name")
    who = who or "Someone"
    kind = entry.get("type")

    if kind == "trade":
        names = [t.get("team_name") or t.get("display_name") for t in entry["teams_involved"]]
        parts = [f"Trade between {' and '.join(n for n in names if n)}"]
        for add in entry["adds"]:
            team = add["to_team"].get("team_name") or add["to_team"].get("display_name")
            parts.append(f"{add['player']['name']} to {team}")
        return "; ".join(parts)

    added = ", ".join(a["player"]["name"] for a in entry["adds"])
    dropped = ", ".join(d["player"]["name"] for d in entry["drops"])
    bid = entry.get("waiver_bid")
    action = "claimed" if kind == "waiver" else "added"
    pieces = []
    if added:
        cost = f" for ${bid} FAAB" if bid else ""
        pieces.append(f"{who} {action} {added}{cost}")
    if dropped:
        pieces.append(f"dropped {dropped}" if pieces else f"{who} dropped {dropped}")
    return ", ".join(pieces) or f"{who} made a {kind} move"


# --- Composite views ---------------------------------------------------------


def league_settings_view(league: dict[str, Any]) -> dict[str, Any]:
    """League configuration translated into plain language."""
    settings = league.get("settings") or {}
    scoring = league.get("scoring_settings") or {}
    roster_positions = league.get("roster_positions") or []

    starting_slots = [p for p in roster_positions if p not in humanize.NON_STARTING_SLOTS]
    slot_counts: dict[str, int] = {}
    for slot in roster_positions:
        slot_counts[slot] = slot_counts.get(slot, 0) + 1

    grouped_scoring: dict[str, list[dict[str, Any]]] = {}
    for key, value in sorted(scoring.items()):
        group = humanize.scoring_group(key)
        grouped_scoring.setdefault(group, []).append(
            {"key": key, "value": value, "description": humanize.scoring_label(key)}
        )

    ppr = scoring.get("rec")
    if ppr is None:
        ppr_label = "Not scored"
    elif ppr >= 1:
        ppr_label = f"Full PPR ({ppr} point per reception)"
    elif ppr > 0:
        ppr_label = f"{ppr} PPR (fractional point per reception)"
    else:
        ppr_label = "Standard (no PPR)"

    return {
        "league_id": league.get("league_id"),
        "name": league.get("name"),
        "season": league.get("season"),
        "season_type": league.get("season_type"),
        "status": league.get("status"),
        "sport": league.get("sport"),
        "total_teams": league.get("total_rosters"),
        "format": {
            "type": humanize.LEAGUE_TYPES.get(
                settings.get("type"), f"Unknown ({settings.get('type')})"
            ),
            "max_keepers": settings.get("max_keepers"),
            "best_ball": bool(settings.get("best_ball")),
            "divisions": settings.get("divisions"),
            "leg": settings.get("leg"),
            "start_week": settings.get("start_week"),
            "description": (
                f"{league.get('total_rosters')} team "
                f"{humanize.LEAGUE_TYPES.get(settings.get('type'), 'league')} league, "
                f"{ppr_label}."
            ),
        },
        "scoring": {
            "ppr": ppr_label,
            "settings_by_group": grouped_scoring,
            "raw": scoring,
        },
        "roster": {
            "positions_in_order": [
                {"slot": slot, "label": humanize.slot_label(slot)}
                for slot in roster_positions
            ],
            "starting_lineup": [
                {"slot": slot, "label": humanize.slot_label(slot)}
                for slot in starting_slots
            ],
            "slot_counts": slot_counts,
            "starters": len(starting_slots),
            "bench_slots": slot_counts.get("BN", 0),
            "ir_slots": settings.get("reserve_slots", slot_counts.get("IR", 0)),
            "taxi_slots": settings.get("taxi_slots", slot_counts.get("TAXI", 0)),
            "total_roster_size": len(roster_positions),
            "description": (
                f"{len(starting_slots)} starters "
                f"({', '.join(starting_slots)}) plus {slot_counts.get('BN', 0)} bench spots."
            ),
        },
        "playoffs": humanize.playoff_summary(settings),
        "trades": {
            "deadline": humanize.trade_deadline_label(settings),
            "deadline_week": settings.get("trade_deadline"),
            "trades_enabled": not bool(settings.get("disable_trades")),
            "draft_pick_trading": bool(settings.get("pick_trading")),
            "review_period_days": settings.get("trade_review_days"),
            "veto_votes_needed": settings.get("veto_votes_needed"),
        },
        "waivers": {
            "type": humanize.waiver_type_label(settings),
            "budget": settings.get("waiver_budget"),
            "clear_days": settings.get("waiver_clear_days"),
            "process_day": humanize.WAIVER_DAYS.get(settings.get("waiver_day_of_week")),
            "daily_waivers": bool(settings.get("daily_waivers")),
            "waiver_type_raw": settings.get("waiver_type"),
        },
        "raw_settings": settings,
    }


async def build_snapshot(
    client: SleeperClient,
    players: PlayerStore,
    league_id: str,
    week: int | None,
    days: int,
) -> dict[str, Any]:
    """The whole league in one readable payload."""
    await players.ensure_fresh()

    state, league, users, rosters = await asyncio.gather(
        client.nfl_state(),
        client.league(league_id),
        client.users(league_id),
        client.rosters(league_id),
    )

    nfl_week = current_week(state)
    target_week = week or nfl_week
    roster_positions = league.get("roster_positions") or []

    weeks = _weeks_to_scan(nfl_week, days)
    matchups_raw, *tx_pages = await asyncio.gather(
        client.matchups(league_id, target_week),
        *[client.transactions(league_id, w) for w in weeks],
    )

    teams = build_teams(users, rosters)
    users_by_id = {u.get("user_id"): u for u in users}
    since_ms = (time.time() - days * 86400) * 1000

    resolved_rosters = [
        resolve_roster(roster, teams.get(roster.get("roster_id"), {}), roster_positions, players)
        for roster in sorted(rosters, key=lambda r: r.get("roster_id") or 0)
    ]

    flat_transactions = [tx for page in tx_pages for tx in page]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "league": {
            "league_id": league.get("league_id"),
            "name": league.get("name"),
            "season": league.get("season"),
            "status": league.get("status"),
            "total_teams": league.get("total_rosters"),
            "scoring_ppr": (league.get("scoring_settings") or {}).get("rec"),
            "roster_positions": roster_positions,
        },
        "nfl_state": {
            "season": state.get("season"),
            "season_type": state.get("season_type"),
            "current_week": nfl_week,
        },
        "week": target_week,
        "standings": _standings(teams),
        "teams": resolved_rosters,
        "matchups": build_matchups(matchups_raw, teams, roster_positions, players),
        "transactions": {
            "days": days,
            "weeks_scanned": weeks,
            "items": build_transactions(
                flat_transactions, teams, players, users_by_id, since_ms
            ),
        },
        "players_cache": players.status(),
    }


def _standings(teams: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        teams.values(),
        key=lambda t: (
            t["record"]["wins"],
            t["record"]["points_for"],
        ),
        reverse=True,
    )
    return [
        {
            "rank": index,
            "roster_id": team["roster_id"],
            "display_name": team["display_name"],
            "team_name": team["team_name"],
            **team["record"],
        }
        for index, team in enumerate(ordered, start=1)
    ]
