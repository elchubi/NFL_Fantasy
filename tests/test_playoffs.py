"""Offline tests for the Monte Carlo playoff-odds simulation."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LEAGUES", "main:1390746710426255360")
os.environ.setdefault("API_KEY", "test-key")

from app import playoffs  # noqa: E402


def test_scoring_profiles_use_population_stdev_once_there_are_enough_games():
    profiles = playoffs.team_scoring_profiles({1: [100.0, 120.0, 110.0]})
    assert profiles[1]["mean"] == 110.0
    assert profiles[1]["games_played"] == 3
    assert profiles[1]["stdev"] > 0


def test_scoring_profiles_fall_back_to_a_fraction_of_the_mean_with_too_little_history():
    profiles = playoffs.team_scoring_profiles({1: [100.0], 2: []})
    # One game: no real variance to measure, so a fraction of the mean stands in.
    assert profiles[1]["stdev"] == round(100.0 * playoffs.DEFAULT_STDEV_FRACTION, 2)
    # Zero games: falls back to the league mean instead of an undefined average.
    assert profiles[2]["mean"] == 100.0


def test_a_dominant_team_almost_always_makes_the_playoffs():
    """4 teams, top 2 make it. Team 1 is 6-0 with a huge scoring edge and low
    variance; team 4 is 0-6 with a weak, low-variance profile. Two games left
    against the middle teams should not be enough to flip either outcome."""
    standings = {
        1: {"wins": 6, "losses": 0, "points_for": 900.0},
        2: {"wins": 3, "losses": 3, "points_for": 700.0},
        3: {"wins": 3, "losses": 3, "points_for": 690.0},
        4: {"wins": 0, "losses": 6, "points_for": 500.0},
    }
    profiles = {
        1: {"mean": 160.0, "stdev": 5.0, "games_played": 6},
        2: {"mean": 115.0, "stdev": 10.0, "games_played": 6},
        3: {"mean": 114.0, "stdev": 10.0, "games_played": 6},
        4: {"mean": 80.0, "stdev": 5.0, "games_played": 6},
    }
    remaining = [[(1, 4), (2, 3)], [(1, 2), (3, 4)]]

    odds = playoffs.simulate_playoff_odds(
        profiles, standings, remaining, playoff_spots=2, trials=1000, seed=42
    )
    assert odds[1] > 0.95
    assert odds[4] < 0.05
    assert odds[1] + odds[2] + odds[3] + odds[4] == 2.0  # exactly 2 spots, every trial


def test_a_bye_created_by_an_odd_number_of_teams_does_not_crash():
    """A roster with no matchup in `remaining_weeks` (a scheduling gap) is
    simply never simulated that week - it should not raise."""
    standings = {1: {"wins": 1, "points_for": 100.0}, 2: {"wins": 1, "points_for": 100.0}}
    profiles = playoffs.team_scoring_profiles({1: [100.0, 100.0], 2: [100.0, 100.0]})
    odds = playoffs.simulate_playoff_odds(
        profiles, standings, remaining_weeks=[[]], playoff_spots=1, trials=50, seed=1
    )
    assert set(odds) == {1, 2}


def test_classify_buckets_by_threshold():
    assert playoffs.classify(0.9) == "buyer"
    assert playoffs.classify(0.75) == "buyer"
    assert playoffs.classify(0.5) == "bubble"
    assert playoffs.classify(0.2) == "seller"
    assert playoffs.classify(0.05) == "seller"
