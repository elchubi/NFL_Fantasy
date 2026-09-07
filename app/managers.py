"""Behavioural profiles of the other managers in the league.

This is the one thing a general-purpose fantasy tool structurally cannot do:
it has no idea who else is in your league. You play the same eleven people for
years, and their habits are data - sitting in Sleeper's transaction and draft
history, free to read, and almost certainly unexamined by them.

What it looks for, and why each one is worth knowing:

    FAAB behaviour    A manager who has never bid above $12 can be beaten with
                      $13 instead of $40.
    Bid timing        Whether they claim early in the week or at the deadline.
    Injury reaction   How fast they cut hurt players - that is the buy-low window.
    Activity          Somebody who barely touches waivers is a source of free
                      talent and the natural trade target.
    Draft tendencies  Positional reaches predict what disappears before your
                      next pick.
    Trade history     Who actually trades, and with whom.

Everything is derived from data already fetched for /snapshot, plus the two
Sleeper draft endpoints.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from app.players import PlayerStore

log = logging.getLogger(__name__)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# A drop this soon after the player appears on an injury report is treated as a
# reaction to the injury rather than an unrelated roster move.
INJURY_REACTION_DAYS = 4


def _epoch_ms_to_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def build_profiles(
    teams: dict[int, dict[str, Any]],
    transactions: list[dict[str, Any]],
    draft_picks: list[dict[str, Any]],
    players: PlayerStore,
    *,
    waiver_budget: int | None = None,
    injury_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One behavioural profile per roster in the league."""
    profiles: dict[int, dict[str, Any]] = {
        roster_id: _empty_profile(team) for roster_id, team in teams.items()
    }

    _apply_transactions(profiles, transactions, players, injury_history or [])
    _apply_draft(profiles, draft_picks, players)

    for profile in profiles.values():
        _summarise(profile, waiver_budget)

    ordered = sorted(profiles.values(), key=lambda p: p["roster_id"])
    return {
        "managers": ordered,
        "league_context": _league_context(ordered),
    }


def _empty_profile(team: dict[str, Any]) -> dict[str, Any]:
    return {
        "roster_id": team.get("roster_id"),
        "display_name": team.get("display_name"),
        "team_name": team.get("team_name"),
        "_bids": [],
        "_won_bids": [],
        "_lost_bids": [],
        "_weekdays": Counter(),
        "_counts": Counter(),
        "_trade_partners": Counter(),
        "_drops": 0,
        "_injury_reaction_days": [],
        "_draft_by_round": defaultdict(list),
        "_draft_positions": Counter(),
        "_first_position": None,
    }


def _apply_transactions(
    profiles: dict[int, dict[str, Any]],
    transactions: list[dict[str, Any]],
    players: PlayerStore,
    injury_history: list[dict[str, Any]],
) -> None:
    # When a player first showed up on an injury report, for the drop-speed read.
    first_listed: dict[str, datetime] = {}
    for row in injury_history:
        name = " ".join(str(row.get("name", "")).lower().split())
        if not name or row.get("status") in (None, "Active"):
            continue
        try:
            moment = datetime.fromisoformat(str(row["captured_at"]))
        except (KeyError, ValueError):
            continue
        if name not in first_listed or moment < first_listed[name]:
            first_listed[name] = moment

    for tx in transactions:
        if tx.get("status") not in (None, "complete", "failed"):
            continue
        kind = tx.get("type")
        moment = _epoch_ms_to_dt(tx.get("status_updated") or tx.get("created"))
        settings = tx.get("settings") or {}
        bid = settings.get("waiver_bid")
        succeeded = tx.get("status") != "failed"

        for roster_id in tx.get("roster_ids") or []:
            profile = profiles.get(roster_id)
            if profile is None:
                continue

            profile["_counts"][kind] += 1
            if succeeded:
                profile["_counts"]["successful"] += 1
            else:
                profile["_counts"]["failed"] += 1

            if moment:
                profile["_weekdays"][WEEKDAYS[moment.weekday()]] += 1

            if kind == "waiver" and bid is not None:
                profile["_bids"].append(bid)
                (profile["_won_bids"] if succeeded else profile["_lost_bids"]).append(bid)

            if kind == "trade":
                for other in tx.get("roster_ids") or []:
                    if other != roster_id:
                        profile["_trade_partners"][other] += 1

            if not succeeded:
                continue

            for player_id, dropping_roster in (tx.get("drops") or {}).items():
                if dropping_roster != roster_id:
                    continue
                profile["_drops"] += 1
                if not moment:
                    continue
                resolved = players.resolve(player_id)
                listed = first_listed.get(" ".join(resolved["name"].lower().split()))
                if listed and moment >= listed:
                    days = (moment - listed).total_seconds() / 86400
                    if days <= INJURY_REACTION_DAYS * 3:
                        profile["_injury_reaction_days"].append(round(days, 2))


def _apply_draft(
    profiles: dict[int, dict[str, Any]],
    draft_picks: list[dict[str, Any]],
    players: PlayerStore,
) -> None:
    by_roster: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for pick in draft_picks:
        roster_id = pick.get("roster_id")
        if roster_id in profiles:
            by_roster[roster_id].append(pick)

    for roster_id, picks in by_roster.items():
        profile = profiles[roster_id]
        picks.sort(key=lambda p: (p.get("round") or 99, p.get("pick_no") or 999))
        for pick in picks:
            metadata = pick.get("metadata") or {}
            position = metadata.get("position") or (
                players.resolve(pick.get("player_id")).get("position")
            )
            if not position:
                continue
            round_number = pick.get("round")
            profile["_draft_positions"][position] += 1
            if round_number:
                profile["_draft_by_round"][position].append(round_number)
            if profile["_first_position"] is None:
                profile["_first_position"] = position


def _summarise(profile: dict[str, Any], waiver_budget: int | None) -> None:
    bids = profile.pop("_bids")
    won = profile.pop("_won_bids")
    lost = profile.pop("_lost_bids")
    weekdays = profile.pop("_weekdays")
    counts = profile.pop("_counts")
    partners = profile.pop("_trade_partners")
    drops = profile.pop("_drops")
    reactions = profile.pop("_injury_reaction_days")
    by_round = profile.pop("_draft_by_round")
    positions = profile.pop("_draft_positions")
    first_position = profile.pop("_first_position")

    contested = len(won) + len(lost)
    profile["waivers"] = {
        "bids_placed": len(bids),
        "typical_bid": round(statistics.median(bids), 1) if bids else None,
        "average_bid": round(statistics.fmean(bids), 1) if bids else None,
        "max_bid": max(bids) if bids else None,
        "total_bid": sum(bids) if bids else 0,
        "claims_won": len(won),
        "claims_lost": len(lost),
        "win_rate": round(len(won) / contested, 2) if contested else None,
        "budget_share_of_max_bid": (
            round(max(bids) / waiver_budget, 2) if bids and waiver_budget else None
        ),
    }
    profile["timing"] = {
        "busiest_day": weekdays.most_common(1)[0][0] if weekdays else None,
        "moves_by_weekday": dict(weekdays.most_common()),
    }
    profile["activity"] = {
        "total_moves": sum(counts[k] for k in ("waiver", "free_agent", "trade")),
        "waivers": counts.get("waiver", 0),
        "free_agents": counts.get("free_agent", 0),
        "trades": counts.get("trade", 0),
        "drops": drops,
        "failed_claims": counts.get("failed", 0),
    }
    profile["injury_reaction"] = {
        "drops_traced_to_an_injury": len(reactions),
        "median_days_to_drop": (
            round(statistics.median(reactions), 1) if reactions else None
        ),
        "note": (
            None
            if reactions
            else "No injury-linked drops on record yet; needs archived injury history."
        ),
    }
    profile["draft"] = {
        "picks_recorded": sum(positions.values()),
        "first_pick_position": first_position,
        "positions_taken": dict(positions.most_common()),
        "average_round_by_position": {
            position: round(statistics.fmean(rounds), 1)
            for position, rounds in sorted(by_round.items())
        },
    }
    profile["trade_partners"] = {str(k): v for k, v in partners.most_common()}


def _league_context(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """League-wide reference points, so one manager reads against the field."""
    max_bids = [p["waivers"]["max_bid"] for p in profiles if p["waivers"]["max_bid"] is not None]
    moves = [p["activity"]["total_moves"] for p in profiles]

    def name(profile: dict[str, Any]) -> str:
        return profile.get("team_name") or profile.get("display_name") or "?"

    by_moves = sorted(profiles, key=lambda p: p["activity"]["total_moves"])
    # Never let the two ends overlap, which they would in a very small league.
    edge = min(3, len(by_moves) // 2)
    return {
        "league_median_max_bid": round(statistics.median(max_bids), 1) if max_bids else None,
        "league_median_moves": round(statistics.median(moves), 1) if moves else None,
        "least_active": [name(p) for p in by_moves[:edge]],
        "most_active": [name(p) for p in reversed(by_moves[len(by_moves) - edge :])] if edge else [],
        "note": (
            "Least active managers are the cheapest trade targets and the least "
            "likely to contest a waiver claim."
        ),
    }
