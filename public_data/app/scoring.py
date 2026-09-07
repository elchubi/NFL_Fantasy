"""Applies a league's own Sleeper `scoring_settings` to nflverse's raw weekly
stat counts, so points reflect what that league actually pays for rather than
nflverse's baked-in standard/PPR `fantasy_points` columns (fixed rules that
rarely match a real league's 0.5 PPR, 6-point passing TDs, TE premium, etc).

Only offense skill-position scoring is computed. Sleeper's kicking, IDP and
defense/special-teams keys have no equivalent raw counting stat in nflverse's
weekly player file, so a league using them gets those keys back in
`not_applied` rather than a plausible-looking total that quietly ignores them.
"""

from __future__ import annotations

from typing import Any

# scoring_settings key -> the raw stat field on one nflverse weekly row.
_LINEAR: dict[str, str] = {
    "pass_yd": "passing_yards",
    "pass_td": "passing_tds",
    "pass_int": "interceptions",
    "pass_2pt": "passing_2pt_conversions",
    "pass_cmp": "completions",
    "pass_att": "pass_attempts",
    "rush_yd": "rushing_yards",
    "rush_td": "rushing_tds",
    "rush_2pt": "rushing_2pt_conversions",
    "rush_att": "carries",
    "rec": "receptions",
    "rec_yd": "receiving_yards",
    "rec_td": "receiving_tds",
    "rec_2pt": "receiving_2pt_conversions",
}

# scoring_settings key -> (yardage field, threshold) for a flat bonus once a
# game's yardage clears the threshold.
_YARDAGE_BONUSES: dict[str, tuple[str, int]] = {
    "bonus_pass_yd_300": ("passing_yards", 300),
    "bonus_pass_yd_400": ("passing_yards", 400),
    "bonus_rush_yd_100": ("rushing_yards", 100),
    "bonus_rush_yd_200": ("rushing_yards", 200),
    "bonus_rec_yd_100": ("receiving_yards", 100),
    "bonus_rec_yd_200": ("receiving_yards", 200),
}

# Per-reception bonus that only applies to tight ends (TE premium).
_TE_RECEPTION_BONUS_KEY = "bonus_rec_te"

SUPPORTED_KEYS: frozenset[str] = frozenset(
    {*_LINEAR, *_YARDAGE_BONUSES, "fum_lost", _TE_RECEPTION_BONUS_KEY}
)

RECENT_WEEKS = 3


def _fumbles_lost(week: dict[str, Any]) -> float:
    return (
        (week.get("sack_fumbles_lost") or 0)
        + (week.get("rushing_fumbles_lost") or 0)
        + (week.get("receiving_fumbles_lost") or 0)
    )


def compute_points(
    week: dict[str, Any], scoring_settings: dict[str, Any], position: str | None = None
) -> dict[str, Any]:
    """Points one week's stat line is worth under `scoring_settings`.

    Returns `{"points": float, "breakdown": {key: points}}`. Keys in
    `scoring_settings` with no raw stat to apply them to are simply skipped
    here - see `unsupported_keys` to report those to a caller once per call
    rather than per week.
    """
    breakdown: dict[str, float] = {}
    total = 0.0
    position = (position or week.get("position") or "").upper()

    for key, weight in (scoring_settings or {}).items():
        if not weight:
            continue
        if key in _LINEAR:
            stat = week.get(_LINEAR[key]) or 0
            if stat:
                breakdown[key] = stat * weight
        elif key in _YARDAGE_BONUSES:
            stat_field, threshold = _YARDAGE_BONUSES[key]
            if (week.get(stat_field) or 0) >= threshold:
                breakdown[key] = weight
        elif key == "fum_lost":
            lost = _fumbles_lost(week)
            if lost:
                breakdown[key] = lost * weight
        elif key == _TE_RECEPTION_BONUS_KEY and position == "TE":
            receptions = week.get("receptions") or 0
            if receptions:
                breakdown[key] = receptions * weight

    total = round(sum(breakdown.values()), 2)
    return {"points": total, "breakdown": {k: round(v, 2) for k, v in breakdown.items()}}


def unsupported_keys(scoring_settings: dict[str, Any]) -> list[str]:
    """Scoring keys with a nonzero weight that this engine cannot apply."""
    return sorted(
        key for key, weight in (scoring_settings or {}).items()
        if weight and key not in SUPPORTED_KEYS
    )


def season_points(player: dict[str, Any], scoring_settings: dict[str, Any]) -> dict[str, Any]:
    """Weekly and season points for one nflverse player record.

    `player` is one entry from `NflverseProvider.season_data()["players"]`:
    `{"position": ..., "weeks": {week: {...raw stat fields...}}}`.
    """
    position = player.get("position")
    weekly: dict[str, float] = {}
    for week_num, row in player.get("weeks", {}).items():
        weekly[str(week_num)] = compute_points(row, scoring_settings, position)["points"]

    ordered_weeks = sorted(weekly, key=int)
    season_total = round(sum(weekly.values()), 2)
    games = len(ordered_weeks)
    recent = ordered_weeks[-RECENT_WEEKS:]
    return {
        "weekly_points": weekly,
        "season_total_points": season_total,
        "season_average_points": round(season_total / games, 2) if games else None,
        "recent_average_points": (
            round(sum(weekly[w] for w in recent) / len(recent), 2) if recent else None
        ),
    }
