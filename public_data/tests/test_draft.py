"""Offline tests for the rookie draft board."""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

from app.draft import _parse_combine, _parse_draft_picks, landing_spot  # noqa: E402
from app.teams import normalise_abbr  # noqa: E402
from tests import fixtures_external as fx  # noqa: E402


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def picks(tmp_path):
    return _parse_draft_picks(_write(tmp_path, "d.csv", fx.DRAFT_PICKS_CSV), 2026)


def test_only_skill_positions_of_the_requested_class_are_kept(picks):
    names = sorted(p["name"] for p in picks.values())
    # The DE is dropped, and so is the 2025 pick.
    assert names == ["Carnell Tate", "Jeremiyah Love", "John Smith", "Kaelon Black"]
    assert all(p["draft"]["season"] == 2026 for p in picks.values())


def test_picks_are_keyed_by_gsis_id_so_they_join_to_sleeper(picks):
    assert "00-0041027" in picks
    assert picks["00-0041027"]["name"] == "Jeremiyah Love"
    # A pick with no gsis_id yet still appears, under a fallback key.
    assert "pfr:BlacKa00" in picks
    assert picks["pfr:BlacKa00"]["gsis_id"] is None


def test_pro_football_reference_team_codes_are_normalised(picks):
    # draft_picks ships GNB/KAN/NWE, which nothing else in this service uses.
    assert picks["00-0041500"]["draft"]["team"] == "GB"
    assert picks["pfr:BlacKa00"]["draft"]["team"] == "KC"
    assert normalise_abbr("NWE") == "NE"


def test_combine_joins_on_either_id(tmp_path, picks):
    combine = _parse_combine(_write(tmp_path, "c.csv", fx.COMBINE_CSV), 2026)
    # Love has both ids; Tate only a cfb id; Smith only a pfr id.
    assert combine["by_pfr"]["LoveJe00"]["forty"] == 4.36
    assert combine["by_cfb"]["carnell-tate-1"]["forty"] == 4.41
    assert combine["by_pfr"]["SmitJo00"]["forty"] == 4.5
    # Empty measurements are dropped rather than stored as None.
    assert "bench" not in combine["by_pfr"]["LoveJe00"]
    # 2025 rows are not in the 2026 class.
    assert "OldPl00" not in combine["by_pfr"]


# --- Landing spot -------------------------------------------------------------


class _Players:
    """Stand-in for PlayerStore: maps gsis_id -> current NFL team."""

    def __init__(self, current_teams):
        self._current = current_teams

    def sleeper_id_for_gsis(self, gsis):
        return gsis if gsis in self._current else None

    def raw(self, pid):
        team = self._current.get(pid)
        return {"team": team} if team else None


def _prior(*players):
    return {
        "season": 2025,
        "players": {
            p["gsis_id"]: {
                "gsis_id": p["gsis_id"],
                "name": p["name"],
                "position": p["position"],
                "team": p["team"],
                "season_averages": {"snap_pct": p["snap_pct"], "targets": p.get("targets")},
            }
            for p in players
        },
    }


def _prospect(team="MIA", position="WR"):
    return {"position": position, "draft": {"team": team}}


def test_departed_incumbents_are_detected_as_vacated_work():
    prior = _prior(
        {"gsis_id": "a", "name": "Left Guy", "position": "WR", "team": "MIA", "snap_pct": 0.80},
        {"gsis_id": "b", "name": "Stayed Guy", "position": "WR", "team": "MIA", "snap_pct": 0.40},
    )
    players = _Players({"a": "DEN", "b": "MIA"})  # a was traded away

    spot = landing_spot(_prospect(), prior, players)
    assert spot["available"] is True
    by_name = {i["name"]: i for i in spot["incumbents"]}
    assert by_name["Left Guy"]["still_on_team"] is False
    assert by_name["Left Guy"]["current_team"] == "DEN"
    assert by_name["Stayed Guy"]["still_on_team"] is True
    # 0.80 of 1.20 total snap workload left.
    assert spot["vacated_share"] == pytest.approx(0.667, abs=1e-3)
    assert "wide open" in spot["opportunity"]


def test_opportunity_wording_tracks_the_size_of_the_opening():
    def share(left, stayed):
        prior = _prior(
            {"gsis_id": "a", "name": "Gone", "position": "WR", "team": "MIA", "snap_pct": left},
            {"gsis_id": "b", "name": "Stays", "position": "WR", "team": "MIA", "snap_pct": stayed},
        )
        return landing_spot(_prospect(), prior, _Players({"a": "DEN", "b": "MIA"}))["opportunity"]

    assert "wide open" in share(0.8, 0.2)
    assert "meaningful opening" in share(0.4, 0.6)
    assert "modest opening" in share(0.1, 0.9)


def test_vacated_share_never_exceeds_one():
    """Three receivers are on the field at once, so their snap percentages sum
    well past 1.0; the reported share has to be a proportion, not that sum."""
    prior = _prior(
        {"gsis_id": "a", "name": "A", "position": "WR", "team": "NYJ", "snap_pct": 0.98},
        {"gsis_id": "b", "name": "B", "position": "WR", "team": "NYJ", "snap_pct": 0.90},
        {"gsis_id": "c", "name": "C", "position": "WR", "team": "NYJ", "snap_pct": 0.67},
    )
    players = _Players({"a": "MIA", "b": "KC", "c": "GB"})  # all three gone

    spot = landing_spot(_prospect(team="NYJ"), prior, players)
    assert spot["vacated_share"] == 1.0
    assert spot["vacated_snap_points"] == pytest.approx(2.55, abs=1e-3)
    assert "wide open" in spot["opportunity"]


def test_a_blocked_landing_spot_reports_zero():
    prior = _prior(
        {"gsis_id": "a", "name": "Star WR", "position": "WR", "team": "LAR", "snap_pct": 0.71},
    )
    spot = landing_spot(_prospect(team="LAR"), prior, _Players({"a": "LAR"}))
    assert spot["vacated_share"] == 0.0
    assert "no vacated work" in spot["opportunity"]
    assert "Star WR returns" in spot["opportunity"]


def test_players_absent_from_sleeper_count_as_gone():
    """Someone out of the league entirely has no Sleeper record at all."""
    prior = _prior(
        {"gsis_id": "retired", "name": "Retired Guy", "position": "TE", "team": "BAL", "snap_pct": 0.6},
    )
    spot = landing_spot(_prospect(team="BAL", position="TE"), prior, _Players({}))
    assert spot["incumbents"][0]["still_on_team"] is False
    assert spot["vacated_share"] == 1.0


def test_other_positions_and_teams_are_not_counted():
    prior = _prior(
        {"gsis_id": "a", "name": "Same team RB", "position": "RB", "team": "MIA", "snap_pct": 0.9},
        {"gsis_id": "b", "name": "Other team WR", "position": "WR", "team": "BUF", "snap_pct": 0.9},
    )
    spot = landing_spot(_prospect(team="MIA", position="WR"), prior, _Players({"a": "MIA", "b": "BUF"}))
    assert spot["incumbents"] == []
    assert spot["vacated_share"] == 0.0


def test_an_unknown_drafting_team_is_reported_not_guessed():
    spot = landing_spot({"position": "WR", "draft": {"team": None}}, _prior(), _Players({}))
    assert spot["available"] is False
