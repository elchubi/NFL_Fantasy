"""Monte Carlo playoff-odds simulation from each team's own scoring history.

Not a projection system: a team's own points_for distribution this season is
the only input, so this only ever answers "given how this team has actually
scored, how does the rest of its schedule play out" - no opponent-specific
matchup modelling, no player-level or injury-adjusted projections. Cheap
enough to be honest about that rather than pretend to more precision than a
season of 12-14 data points supports.
"""

from __future__ import annotations

import random
import statistics
from typing import Any

# Used when a team has too little history to measure its own week-to-week
# swing (0 or 1 games played) - a fraction of its own mean rather than a
# fixed number, so a high- and low-scoring team both get a plausible spread.
DEFAULT_STDEV_FRACTION = 0.18
MIN_STDEV = 1.0
TRIALS = 3000


def team_scoring_profiles(weekly_scores: dict[Any, list[float]]) -> dict[Any, dict[str, float]]:
    """roster_id -> {"mean", "stdev", "games_played"}, from each team's own
    weekly scores so far this season."""
    league_scores = [s for scores in weekly_scores.values() for s in scores]
    league_mean = statistics.mean(league_scores) if league_scores else 100.0

    profiles: dict[Any, dict[str, float]] = {}
    for roster_id, scores in weekly_scores.items():
        mean = statistics.mean(scores) if scores else league_mean
        stdev = statistics.pstdev(scores) if len(scores) >= 2 else mean * DEFAULT_STDEV_FRACTION
        profiles[roster_id] = {
            "mean": round(mean, 2),
            "stdev": round(max(stdev, MIN_STDEV), 2),
            "games_played": len(scores),
        }
    return profiles


def simulate_playoff_odds(
    profiles: dict[Any, dict[str, float]],
    standings: dict[Any, dict[str, float]],
    remaining_weeks: list[list[tuple[Any, Any]]],
    playoff_spots: int,
    trials: int = TRIALS,
    seed: int | None = None,
) -> dict[Any, float]:
    """The share of `trials` simulated seasons in which each roster finishes
    in the top `playoff_spots` by (wins, points_for) - Sleeper's own
    tie-break - after playing out `remaining_weeks` from its current record.

    `standings` is roster_id -> {"wins", "losses", "ties", "points_for"} as
    of right now. `remaining_weeks` is one list of (roster_a, roster_b) pairs
    per week left in the regular season, in order.
    """
    rng = random.Random(seed)
    made_playoffs = {rid: 0 for rid in standings}

    for _ in range(trials):
        wins = {rid: standings[rid].get("wins", 0) for rid in standings}
        points_for = {rid: standings[rid].get("points_for", 0.0) for rid in standings}

        for week_pairs in remaining_weeks:
            for a, b in week_pairs:
                if a not in profiles or b not in profiles:
                    continue
                score_a = rng.gauss(profiles[a]["mean"], profiles[a]["stdev"])
                score_b = rng.gauss(profiles[b]["mean"], profiles[b]["stdev"])
                points_for[a] += score_a
                points_for[b] += score_b
                if score_a > score_b:
                    wins[a] += 1
                elif score_b > score_a:
                    wins[b] += 1

        ranked = sorted(standings, key=lambda rid: (wins[rid], points_for[rid]), reverse=True)
        for rid in ranked[:playoff_spots]:
            made_playoffs[rid] += 1

    return {rid: round(count / trials, 4) for rid, count in made_playoffs.items()}


def classify(odds: float) -> str:
    """A blunt buy/sell/hold read on one team's playoff odds."""
    if odds >= 0.75:
        return "buyer"
    if odds <= 0.20:
        return "seller"
    return "bubble"
