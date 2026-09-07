"""Which teams are structurally forced to act, and when.

A manager whose only two startable running backs share a bye week has to make
a move, whether he has worked that out yet or not. Knowing it before he does
changes what you can ask for in a trade.

Everything here is computed from data the service already holds: the resolved
rosters from /snapshot, bye weeks derived from the nflverse schedule, and the
injury statuses already on each player.
"""

from __future__ import annotations

import logging
from typing import Any

from app.humanize import NON_STARTING_SLOTS
from app.teams import normalise_abbr

log = logging.getLogger(__name__)

# Which real positions can fill each lineup slot.
SLOT_ELIGIBILITY: dict[str, tuple[str, ...]] = {
    "QB": ("QB",),
    "RB": ("RB",),
    "WR": ("WR",),
    "TE": ("TE",),
    "K": ("K",),
    "DEF": ("DEF",),
    "FLEX": ("RB", "WR", "TE"),
    "WRRB_FLEX": ("RB", "WR"),
    "REC_FLEX": ("WR", "TE"),
    "SUPER_FLEX": ("QB", "RB", "WR", "TE"),
    "IDP_FLEX": ("DL", "LB", "DB"),
    "DL": ("DL",),
    "LB": ("LB",),
    "DB": ("DB",),
}

# Statuses that mean the player is not available this week.
OUT_STATUSES = {"Out", "IR", "PUP", "Suspended", "Doubtful", "NA"}
QUESTIONABLE_STATUSES = {"Questionable", "Day-To-Day"}

# Positions everyone streams: carrying exactly one is correct roster
# construction, and a replacement is always sitting on waivers for nothing. If
# these counted as thin, almost every team in the league would read as under
# pressure and the signal would be worthless.
STREAMED_POSITIONS = {"K", "DEF"}


def analyse_league(
    teams: list[dict[str, Any]],
    roster_positions: list[str],
    byes: dict[str, int | None],
    week: int,
    *,
    horizon: int = 3,
) -> dict[str, Any]:
    """Pressure report for every team, most pressured first."""
    slots = [p for p in roster_positions if p not in NON_STARTING_SLOTS]
    reports = [
        analyse_team(team, slots, byes, week, horizon=horizon) for team in teams
    ]
    reports.sort(key=lambda r: r["pressure_score"], reverse=True)

    forced = [r for r in reports if r["pressure_score"] >= 2]
    return {
        "week": week,
        "horizon_weeks": list(range(week, week + horizon)),
        "teams": reports,
        "under_pressure": [
            {
                "team_name": r["team_name"],
                "display_name": r["display_name"],
                "pressure_score": r["pressure_score"],
                "headline": r["headline"],
            }
            for r in forced
        ],
        "note": (
            "A team under pressure has to move before you do, which is leverage "
            "in any trade you open with them."
        ),
    }


def analyse_team(
    team: dict[str, Any],
    slots: list[str],
    byes: dict[str, int | None],
    week: int,
    *,
    horizon: int = 3,
) -> dict[str, Any]:
    roster = _roster_players(team)
    required = _slot_requirements(slots)

    unavailable_now = [p for p in roster if _is_out(p)]
    questionable = [p for p in roster if _is_questionable(p)]

    weeks_ahead = list(range(week, week + horizon))
    by_week: list[dict[str, Any]] = []
    for target_week in weeks_ahead:
        available = [
            p
            for p in roster
            if not _is_out(p) and byes.get(normalise_abbr(p.get("nfl_team"))) != target_week
        ]
        shortfalls = _shortfalls(available, required)
        on_bye = [
            p["name"]
            for p in roster
            if byes.get(normalise_abbr(p.get("nfl_team"))) == target_week
        ]
        by_week.append(
            {
                "week": target_week,
                "players_on_bye": on_bye,
                "shortfalls": shortfalls,
                "can_field_a_lineup": not shortfalls,
            }
        )

    thin = _thin_positions(roster, required)
    score, headline = _score(by_week, unavailable_now, thin, questionable)

    return {
        "roster_id": team.get("roster_id"),
        "display_name": team.get("display_name"),
        "team_name": team.get("team_name"),
        "pressure_score": score,
        "headline": headline,
        "unavailable_now": [
            {"name": p["name"], "position": p.get("position"), "status": p.get("injury_status")}
            for p in unavailable_now
        ],
        "questionable": [
            {"name": p["name"], "position": p.get("position"), "status": p.get("injury_status")}
            for p in questionable
        ],
        "thin_positions": thin,
        "by_week": by_week,
    }


# --- Internals ----------------------------------------------------------------


def _roster_players(team: dict[str, Any]) -> list[dict[str, Any]]:
    entries = [s.get("player") for s in team.get("starters", [])]
    entries += team.get("bench", []) + team.get("injured_reserve", [])
    return [p for p in entries if p]


def _slot_requirements(slots: list[str]) -> list[tuple[str, tuple[str, ...]]]:
    return [(slot, SLOT_ELIGIBILITY.get(slot, (slot,))) for slot in slots]


def _is_out(player: dict[str, Any]) -> bool:
    return (player.get("injury_status") or "") in OUT_STATUSES


def _is_questionable(player: dict[str, Any]) -> bool:
    return (player.get("injury_status") or "") in QUESTIONABLE_STATUSES


def _shortfalls(
    available: list[dict[str, Any]], required: list[tuple[str, tuple[str, ...]]]
) -> list[dict[str, Any]]:
    """Greedily fill the lineup, hardest-to-fill slots first.

    Dedicated slots are filled before flex slots, since a flex can be covered by
    several positions and a dedicated slot cannot.
    """
    pool = list(available)
    order = sorted(required, key=lambda item: len(item[1]))
    missing: list[dict[str, Any]] = []

    for slot, eligible in order:
        pick = next((p for p in pool if p.get("position") in eligible), None)
        if pick is None:
            missing.append({"slot": slot, "eligible_positions": list(eligible)})
        else:
            pool.remove(pick)
    return missing


def _thin_positions(
    roster: list[dict[str, Any]], required: list[tuple[str, tuple[str, ...]]]
) -> list[dict[str, Any]]:
    """Positions with no healthy cover beyond the starters they must fill."""
    dedicated: dict[str, int] = {}
    for slot, eligible in required:
        if len(eligible) == 1:
            dedicated[eligible[0]] = dedicated.get(eligible[0], 0) + 1

    thin = []
    for position, needed in sorted(dedicated.items()):
        if position in STREAMED_POSITIONS:
            continue
        healthy = [p for p in roster if p.get("position") == position and not _is_out(p)]
        if len(healthy) <= needed:
            thin.append(
                {
                    "position": position,
                    "healthy": len(healthy),
                    "starters_required": needed,
                    "spare": len(healthy) - needed,
                }
            )
    return thin


def _score(
    by_week: list[dict[str, Any]],
    unavailable_now: list[dict[str, Any]],
    thin: list[dict[str, Any]],
    questionable: list[dict[str, Any]],
) -> tuple[int, str]:
    """A blunt 0-5 urgency score, plus the one line that explains it."""
    score = 0
    reasons: list[str] = []

    broken = [w for w in by_week if not w["can_field_a_lineup"]]
    if broken:
        score += 3
        first = broken[0]
        slots = ", ".join(s["slot"] for s in first["shortfalls"])
        reasons.append(f"cannot fill {slots} in week {first['week']}")

    # Only an actual shortfall scores. Carrying exactly enough at a position is
    # worth reporting as fragility, but it is how most rosters look, so scoring
    # it would flag the whole league and say nothing.
    short = [t for t in thin if t["spare"] < 0]
    if short:
        score += 1
        reasons.append("short at " + ", ".join(t["position"] for t in short))

    if len(unavailable_now) >= 2:
        score += 1
        reasons.append(f"{len(unavailable_now)} players unavailable")
    if len(questionable) >= 3:
        score += 1
        reasons.append(f"{len(questionable)} questionable")

    if not reasons:
        return 0, "No structural pressure: can field a full lineup with cover."
    return min(score, 5), "; ".join(reasons) + "."
