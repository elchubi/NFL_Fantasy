"""Offline tests for applying a league's own scoring_settings to nflverse's
raw weekly stat counts."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("API_KEY", "test-key")

from app import scoring  # noqa: E402

HALF_PPR = {
    "pass_yd": 0.04,
    "pass_td": 4,
    "pass_int": -2,
    "rush_yd": 0.1,
    "rush_td": 6,
    "rec": 0.5,
    "rec_yd": 0.1,
    "rec_td": 6,
    "fum_lost": -2,
}


def test_a_qb_line_is_scored_under_the_leagues_own_weights():
    week = {"passing_yards": 300, "passing_tds": 3, "interceptions": 1, "rushing_yards": 10}
    result = scoring.compute_points(week, HALF_PPR, position="QB")
    # 300*0.04 + 3*4 - 2*1 + 10*0.1 = 12 + 12 - 2 + 1 = 23
    assert result["points"] == 23.0
    assert result["breakdown"]["pass_td"] == 12.0
    assert result["breakdown"]["pass_int"] == -2.0


def test_receptions_only_count_under_ppr_weights_that_are_actually_set():
    standard = {k: v for k, v in HALF_PPR.items() if k != "rec"}
    week = {"receptions": 6, "receiving_yards": 80}
    assert scoring.compute_points(week, standard)["points"] == 8.0  # 80*0.1, no PPR
    assert scoring.compute_points(week, HALF_PPR)["points"] == 11.0  # + 6*0.5


def test_fumbles_lost_are_summed_across_every_source():
    week = {"sack_fumbles_lost": 1, "rushing_fumbles_lost": 1, "receiving_fumbles_lost": 0}
    result = scoring.compute_points(week, HALF_PPR)
    assert result["breakdown"]["fum_lost"] == -4.0


def test_yardage_bonuses_are_flat_once_the_threshold_is_cleared():
    settings = {"rush_yd": 0.1, "bonus_rush_yd_100": 3}
    short = scoring.compute_points({"rushing_yards": 99}, settings)
    long = scoring.compute_points({"rushing_yards": 100}, settings)
    assert "bonus_rush_yd_100" not in short["breakdown"]
    assert long["breakdown"]["bonus_rush_yd_100"] == 3.0


def test_te_premium_only_applies_to_tight_ends():
    settings = {"rec": 0.5, "bonus_rec_te": 0.5}
    week = {"receptions": 4}
    te = scoring.compute_points(week, settings, position="TE")
    wr = scoring.compute_points(week, settings, position="WR")
    assert te["points"] == 4.0  # 4*0.5 (rec) + 4*0.5 (TE bonus)
    assert wr["points"] == 2.0  # 4*0.5 (rec) only


def test_unsupported_keys_are_reported_rather_than_silently_dropped():
    settings = {"rec": 0.5, "pts_allow_0": 10, "idp_tkl": 1}
    assert scoring.unsupported_keys(settings) == ["idp_tkl", "pts_allow_0"]
    # A zero-weighted key the league never actually uses is not "unsupported".
    assert scoring.unsupported_keys({"idp_tkl": 0}) == []


def test_season_points_aggregates_across_weeks():
    player = {
        "position": "RB",
        "weeks": {
            "1": {"rushing_yards": 100, "rushing_tds": 1},
            "2": {"rushing_yards": 50},
            "3": {"rushing_yards": 80, "rushing_tds": 1},
        },
    }
    settings = {"rush_yd": 0.1, "rush_td": 6}
    result = scoring.season_points(player, settings)
    assert result["weekly_points"] == {"1": 16.0, "2": 5.0, "3": 14.0}
    assert result["season_total_points"] == 35.0
    assert result["season_average_points"] == round(35.0 / 3, 2)
    # Exactly 3 games played: the recent window is the whole season.
    assert result["recent_average_points"] == result["season_average_points"]
