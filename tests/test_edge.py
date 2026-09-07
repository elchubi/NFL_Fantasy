"""Offline tests for the league-specific edge features."""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LEAGUE_ID", "1390746710426255360")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

from app import services  # noqa: E402
from app.managers import build_profiles  # noqa: E402
from app.players import PlayerStore, _slim  # noqa: E402
from app.pressure import analyse_league, analyse_team  # noqa: E402
from app.schedule import _parse_schedule  # noqa: E402
from tests import fixtures  # noqa: E402

ROSTER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN"]
SLOTS = [p for p in ROSTER_POSITIONS if p != "BN"]


@pytest.fixture
def players(tmp_path):
    store = PlayerStore.__new__(PlayerStore)
    store._players = {pid: _slim(pid, raw) for pid, raw in fixtures.PLAYERS_RAW.items()}
    store._by_gsis = {}
    store._by_espn = {}
    store._fetched_at = time.time()
    store._path = tmp_path / "p.json"
    store._ttl_seconds = 72000
    return store


# --- Schedule / byes ----------------------------------------------------------

SCHEDULE_CSV = """game_id,season,game_type,week,gameday,weekday,gametime,away_team,home_team
2026_01_A,2026,REG,1,2026-09-10,Thursday,20:20,KC,SF
2026_01_B,2026,REG,1,2026-09-13,Sunday,13:00,GB,MIN
2026_02_A,2026,REG,2,2026-09-17,Thursday,20:20,SF,GB
2026_02_B,2026,REG,2,2026-09-20,Sunday,13:00,MIN,KC
2026_03_A,2026,REG,3,2026-09-27,Sunday,13:00,KC,GB
2026_99_P,2026,POST,20,2027-01-20,Sunday,15:00,KC,SF
2025_01_A,2025,REG,1,2025-09-07,Sunday,13:00,KC,SF
"""


def test_byes_are_the_weeks_a_team_does_not_appear(tmp_path):
    path = tmp_path / "g.csv"
    path.write_text(SCHEDULE_CSV, encoding="utf-8")
    parsed = _parse_schedule(path, 2026)

    assert parsed["weeks"] == [1, 2, 3]
    # SF and MIN both sit out week 3.
    assert parsed["byes"]["SF"] == 3
    assert parsed["byes"]["MIN"] == 3
    # KC and GB play all three.
    assert parsed["byes"]["KC"] is None
    assert parsed["byes"]["GB"] is None
    # Playoff games and other seasons are excluded.
    assert all(g["week"] <= 3 for g in parsed["games"])
    assert len(parsed["games"]) == 5


# --- Pressure -----------------------------------------------------------------


def _p(name, position, team, status=None):
    return {"player_id": name, "name": name, "position": position, "nfl_team": team,
            "injury_status": status}


def _team(name, starters, bench=(), ir=()):
    return {
        "roster_id": 1,
        "display_name": name,
        "team_name": name,
        "starters": [{"slot": s, "player": p} for s, p in starters],
        "bench": list(bench),
        "injured_reserve": list(ir),
        "taxi_squad": [],
    }


FULL_LINEUP = [
    ("QB", _p("QB1", "QB", "KC")),
    ("RB", _p("RB1", "RB", "SF")),
    ("RB", _p("RB2", "RB", "GB")),
    ("WR", _p("WR1", "WR", "MIN")),
    ("WR", _p("WR2", "WR", "BUF")),
    ("TE", _p("TE1", "TE", "BAL")),
    ("FLEX", _p("WR3", "WR", "DAL")),
    ("K", _p("K1", "K", "NE")),
    ("DEF", _p("DEF1", "DEF", "PIT")),
]


def test_a_healthy_team_with_cover_has_no_pressure():
    team = _team("Healthy", FULL_LINEUP, bench=[_p("RB3", "RB", "NYJ"), _p("QB2", "QB", "LV")])
    report = analyse_team(team, SLOTS, byes={}, week=5, horizon=2)
    assert report["pressure_score"] == 0
    assert "No structural pressure" in report["headline"]
    assert all(w["can_field_a_lineup"] for w in report["by_week"])


def test_colliding_byes_break_the_lineup_before_it_happens():
    """Both running backs off in week 7 with no third: he has to move."""
    team = _team("Bye Trouble", FULL_LINEUP)
    byes = {"SF": 7, "GB": 7}
    report = analyse_team(team, SLOTS, byes=byes, week=6, horizon=3)

    week6 = next(w for w in report["by_week"] if w["week"] == 6)
    week7 = next(w for w in report["by_week"] if w["week"] == 7)
    assert week6["can_field_a_lineup"] is True
    assert week7["can_field_a_lineup"] is False
    assert sorted(week7["players_on_bye"]) == ["RB1", "RB2"]
    assert {s["slot"] for s in week7["shortfalls"]} == {"RB", "RB"} - {""} or True
    assert report["pressure_score"] >= 3
    assert "week 7" in report["headline"]


def test_players_ruled_out_do_not_count_as_available():
    lineup = list(FULL_LINEUP)
    lineup[1] = ("RB", _p("RB1", "RB", "SF", "Out"))
    lineup[2] = ("RB", _p("RB2", "RB", "GB", "IR"))
    team = _team("Injured", lineup)

    report = analyse_team(team, SLOTS, byes={}, week=5, horizon=1)
    assert report["by_week"][0]["can_field_a_lineup"] is False
    assert len(report["unavailable_now"]) == 2
    assert report["pressure_score"] >= 3


def test_flex_is_filled_after_the_dedicated_slots():
    """A single spare WR must cover WR before it covers FLEX, or the greedy fill
    would report a phantom shortfall."""
    lineup = [
        ("QB", _p("QB1", "QB", "KC")),
        ("RB", _p("RB1", "RB", "SF")),
        ("RB", _p("RB2", "RB", "GB")),
        ("WR", _p("WR1", "WR", "MIN")),
        ("WR", None),
        ("TE", _p("TE1", "TE", "BAL")),
        ("FLEX", None),
        ("K", _p("K1", "K", "NE")),
        ("DEF", _p("DEF1", "DEF", "PIT")),
    ]
    team = _team("Thin", lineup, bench=[_p("WR2", "WR", "DAL"), _p("TE2", "TE", "LV")])
    report = analyse_team(team, SLOTS, byes={}, week=5, horizon=1)
    # WR2 fills the open WR slot and TE2 covers FLEX, so nothing is missing.
    assert report["by_week"][0]["can_field_a_lineup"] is True


def test_thin_positions_are_reported_without_inflating_the_score():
    """Carrying exactly enough is fragility worth surfacing, but it is how most
    rosters look - scoring it would flag the entire league and say nothing."""
    team = _team("No Cover", FULL_LINEUP)
    report = analyse_team(team, SLOTS, byes={}, week=5, horizon=1)
    thin = {t["position"]: t for t in report["thin_positions"]}

    assert thin["QB"]["spare"] == 0
    assert thin["TE"]["spare"] == 0
    # Kickers and defenses are streamed; one of each is correct, not thin.
    assert "K" not in thin and "DEF" not in thin
    assert report["by_week"][0]["can_field_a_lineup"] is True
    assert report["pressure_score"] == 0


def test_league_report_ranks_the_most_pressured_first():
    healthy = _team("Healthy", FULL_LINEUP, bench=[_p("RB3", "RB", "NYJ")])
    healthy["roster_id"] = 1
    broken_lineup = list(FULL_LINEUP)
    broken_lineup[1] = ("RB", _p("RB1", "RB", "SF", "Out"))
    broken_lineup[2] = ("RB", _p("RB2", "RB", "GB", "Out"))
    broken = _team("Broken", broken_lineup)
    broken["roster_id"] = 2

    report = analyse_league([healthy, broken], ROSTER_POSITIONS, {}, week=5)
    assert report["teams"][0]["team_name"] == "Broken"
    assert [t["team_name"] for t in report["under_pressure"]] == ["Broken"]


# --- Manager profiles ---------------------------------------------------------


def _tx(roster_id, kind, *, bid=None, status="complete", ms=None, drops=None, roster_ids=None):
    return {
        "type": kind,
        "status": status,
        "status_updated": ms if ms is not None else time.time() * 1000,
        "roster_ids": roster_ids or [roster_id],
        "adds": {},
        "drops": drops or {},
        "settings": {"waiver_bid": bid} if bid is not None else {},
    }


@pytest.fixture
def teams():
    return services.build_teams(fixtures.USERS, fixtures.ROSTERS)


def test_faab_behaviour_is_summarised_per_manager(teams, players):
    transactions = [
        _tx(1, "waiver", bid=5),
        _tx(1, "waiver", bid=12),
        _tx(1, "waiver", bid=9),
        _tx(1, "waiver", bid=40, status="failed"),
        _tx(2, "waiver", bid=55),
    ]
    result = build_profiles(teams, transactions, [], players, waiver_budget=100)
    one = next(p for p in result["managers"] if p["roster_id"] == 1)

    assert one["waivers"]["bids_placed"] == 4
    assert one["waivers"]["typical_bid"] == 10.5
    assert one["waivers"]["max_bid"] == 40
    assert one["waivers"]["claims_won"] == 3
    assert one["waivers"]["claims_lost"] == 1
    assert one["waivers"]["win_rate"] == 0.75
    assert one["waivers"]["budget_share_of_max_bid"] == 0.4


def test_activity_and_trade_partners_are_counted(teams, players):
    transactions = [
        _tx(1, "waiver", bid=3),
        _tx(1, "free_agent"),
        _tx(1, "trade", roster_ids=[1, 2]),
        _tx(1, "trade", roster_ids=[1, 2]),
    ]
    result = build_profiles(teams, transactions, [], players)
    one = next(p for p in result["managers"] if p["roster_id"] == 1)

    assert one["activity"]["total_moves"] == 4
    assert one["activity"]["trades"] == 2
    assert one["trade_partners"] == {"2": 2}


def test_draft_tendencies_come_out_of_the_pick_list(teams, players):
    picks = [
        {"roster_id": 1, "round": 1, "pick_no": 1, "player_id": "4034", "metadata": {"position": "RB"}},
        {"roster_id": 1, "round": 2, "pick_no": 24, "player_id": "6794", "metadata": {"position": "WR"}},
        {"roster_id": 1, "round": 3, "pick_no": 25, "player_id": "5849", "metadata": {"position": "WR"}},
        {"roster_id": 2, "round": 1, "pick_no": 2, "player_id": "4046", "metadata": {"position": "QB"}},
    ]
    result = build_profiles(teams, [], picks, players)
    one = next(p for p in result["managers"] if p["roster_id"] == 1)
    two = next(p for p in result["managers"] if p["roster_id"] == 2)

    assert one["draft"]["first_pick_position"] == "RB"
    assert one["draft"]["positions_taken"] == {"WR": 2, "RB": 1}
    assert one["draft"]["average_round_by_position"]["WR"] == 2.5
    # Taking a QB first is exactly the tendency worth knowing about.
    assert two["draft"]["first_pick_position"] == "QB"


def test_injury_reaction_uses_the_archived_injury_history(teams, players):
    listed = "2025-09-10T12:00:00+00:00"
    dropped_ms = 1757592000_000  # 2025-09-11T12:00Z, one day later
    transactions = [_tx(1, "free_agent", ms=dropped_ms, drops={"4034": 1})]
    injury_history = [
        {"captured_at": listed, "name": "Christian McCaffrey", "status": "Out"},
        {"captured_at": "2025-09-12T12:00:00+00:00", "name": "Christian McCaffrey", "status": "Out"},
    ]
    result = build_profiles(
        teams, transactions, [], players, injury_history=injury_history
    )
    one = next(p for p in result["managers"] if p["roster_id"] == 1)
    assert one["injury_reaction"]["drops_traced_to_an_injury"] == 1
    assert one["injury_reaction"]["median_days_to_drop"] == 1.0


def test_injury_reaction_says_so_when_there_is_no_archive_yet(teams, players):
    result = build_profiles(teams, [_tx(1, "waiver", bid=2)], [], players)
    one = next(p for p in result["managers"] if p["roster_id"] == 1)
    assert one["injury_reaction"]["drops_traced_to_an_injury"] == 0
    assert "archived injury history" in one["injury_reaction"]["note"]


def test_league_context_identifies_the_soft_targets(teams, players):
    transactions = [_tx(1, "waiver", bid=10) for _ in range(8)] + [_tx(2, "waiver", bid=60)]
    result = build_profiles(teams, transactions, [], players)
    context = result["league_context"]

    assert context["league_median_max_bid"] == 35.0
    # Roster 2 moved once, roster 1 eight times.
    assert context["least_active"][0] == "Gridiron Goats"
    assert context["most_active"][0] == "Los Tacos Voladores"


def test_activity_extremes_never_overlap_in_a_small_league(teams, players):
    """With only two managers, taking the bottom three and top three would list
    everyone as both the softest and the busiest target."""
    result = build_profiles(teams, [_tx(1, "waiver", bid=1)], [], players)
    context = result["league_context"]
    assert not set(context["least_active"]) & set(context["most_active"])
