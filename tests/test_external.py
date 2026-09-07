"""Offline tests for the four external data sources."""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LEAGUE_ID", "1390746710426255360")
os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402

from app import espn, nflverse, odds, weather  # noqa: E402
from app.cache import KeyedDiskCache  # noqa: E402
from app.enrichment import parse_includes, rostered_players, rostered_teams  # noqa: E402
from app.teams import STADIUMS, nfl_team_abbr, normalise_abbr  # noqa: E402
from tests import fixtures_external as fx  # noqa: E402


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# --- nflverse -----------------------------------------------------------------


def test_weekly_stats_are_keyed_by_gsis_id(tmp_path):
    parsed = nflverse._parse_weekly_stats(
        _write(tmp_path, "w.csv", fx.STATS_PLAYER_WEEK_CSV)
    )
    assert set(parsed) == {"00-0034796", "00-0036322"}
    cmc = parsed["00-0034796"]
    assert cmc["name"] == "Christian McCaffrey"
    week1 = cmc["weeks"]["1"]
    assert week1["carries"] == 18.0
    assert week1["target_share"] == 0.21
    assert week1["fantasy_points_ppr"] == 26.0
    # "NA" is Sleeper-free nflverse's null; it must not become a string.
    assert week1["passing_epa"] is None


def test_snap_counts_are_rekeyed_through_the_pfr_crosswalk(tmp_path):
    crosswalk = nflverse._parse_crosswalk(
        _write(tmp_path, "p.csv", fx.PLAYERS_CROSSWALK_CSV)
    )
    assert crosswalk == {"McCaCh01": "00-0034796", "JeffJu00": "00-0036322"}

    snaps = nflverse._parse_snap_counts(
        _write(tmp_path, "s.csv", fx.SNAP_COUNTS_CSV), crosswalk
    )
    # snap_counts ships pfr ids, so without the crosswalk nothing would join.
    assert snaps[("00-0034796", "1")]["snap_pct"] == 0.82
    assert snaps[("00-0036322", "2")]["offense_snaps"] == 40.0


def test_snap_counts_without_a_crosswalk_are_dropped_not_mismatched(tmp_path):
    snaps = nflverse._parse_snap_counts(_write(tmp_path, "s.csv", fx.SNAP_COUNTS_CSV), {})
    assert snaps == {}


def test_red_zone_touches_come_from_play_by_play(tmp_path):
    rz = nflverse._parse_red_zone(_write(tmp_path, "pbp.csv", fx.PBP_CSV))
    cmc_w1 = rz[("00-0034796", "1")]
    # Two carries inside the 20; the 45-yard-line carry is not red zone.
    assert cmc_w1["red_zone_carries"] == 2
    assert cmc_w1["red_zone_tds"] == 1
    assert cmc_w1["red_zone_touches"] == 2

    jj_w1 = rz[("00-0036322", "1")]
    assert jj_w1["red_zone_targets"] == 2
    assert jj_w1["red_zone_receptions"] == 1
    assert jj_w1["red_zone_tds"] == 1


def test_summary_exposes_the_trend_against_the_season(tmp_path):
    players = nflverse._parse_weekly_stats(
        _write(tmp_path, "w.csv", fx.STATS_PLAYER_WEEK_CSV)
    )
    crosswalk = nflverse._parse_crosswalk(_write(tmp_path, "p.csv", fx.PLAYERS_CROSSWALK_CSV))
    for (gsis, week), snap in nflverse._parse_snap_counts(
        _write(tmp_path, "s.csv", fx.SNAP_COUNTS_CSV), crosswalk
    ).items():
        players[gsis]["weeks"][week].update(snap)

    provider = nflverse.NflverseProvider.__new__(nflverse.NflverseProvider)
    jefferson = players["00-0036322"]
    provider._summarise(jefferson)

    assert jefferson["games"] == 5
    assert jefferson["season_totals"]["targets"] == 41.0
    assert jefferson["season_averages"]["snap_pct"] == pytest.approx(0.64, abs=1e-3)
    # Snaps and target share fall away over the last three weeks; that drop is
    # the whole point of tracking the trend.
    assert jefferson["trend"]["snap_pct"] < 0
    assert jefferson["trend"]["target_share"] < 0
    assert "snap share trending down" in jefferson["role_note"]
    assert "target share trending down" in jefferson["role_note"]

    nflverse._compact(jefferson)
    # Zero and null fields are dropped to keep the cache small.
    assert "receiving_tds" not in jefferson["weeks"]["2"]
    assert jefferson["weeks"]["2"]["week"] == 2


def test_trend_is_withheld_until_there_are_enough_games(tmp_path):
    """A 2-game sample would otherwise report a flat trend, which reads as
    "role is stable" when it actually means "not enough data"."""
    players = nflverse._parse_weekly_stats(
        _write(tmp_path, "w.csv", fx.STATS_PLAYER_WEEK_CSV)
    )
    provider = nflverse.NflverseProvider.__new__(nflverse.NflverseProvider)
    cmc = players["00-0034796"]
    provider._summarise(cmc)

    assert cmc["games"] == 2
    assert cmc["trend"] == {}
    assert "needs more than 3" in cmc["trend_note"]
    assert cmc["role_note"] is None


# --- The Odds API -------------------------------------------------------------


def test_odds_are_flattened_to_a_consensus_line():
    game = odds.parse_game(fx.ODDS_PAYLOAD[0])
    assert game["home_team_abbr"] == "KC"
    assert game["away_team_abbr"] == "DET"
    # Median across the two books: -8.5 and -9.5.
    assert game["home_spread"] == -9.0
    assert game["total"] == 50.0
    assert game["favourite_abbr"] == "KC"
    assert game["spread"] == 9.0
    assert game["moneyline"]["home"] == -350
    assert game["implied_team_totals"] == {"favourite": 29.5, "underdog": 20.5}
    assert "run-heavy late" in game["game_script"]


def test_odds_survive_a_game_with_no_markets():
    game = odds.parse_game(
        {"id": "x", "home_team": "Chicago Bears", "away_team": "Green Bay Packers"}
    )
    assert game["favourite"] is None
    assert game["total"] is None
    assert game["game_script"] is None


def test_kickoff_maps_to_an_nfl_week():
    # 2025 week 1 Thursday is 2025-09-04.
    assert odds._week_from_kickoff("2025-09-07T17:00:00Z") == 1
    assert odds._week_from_kickoff("2025-09-14T17:00:00Z") == 2
    assert odds._week_from_kickoff("2025-12-28T17:00:00Z") == 17
    assert odds._week_from_kickoff(None) is None
    assert odds._week_from_kickoff("not-a-date") is None


# --- ESPN ---------------------------------------------------------------------


def test_practice_participation_is_pulled_out_of_the_comment():
    items = espn.parse_injuries(fx.ESPN_INJURIES)
    assert len(items) == 2
    cmc = items[0]
    assert cmc["espn_id"] == "3117251"
    assert cmc["status"] == "Questionable"
    assert cmc["practice_participation"] == "limited"
    assert cmc["injury_type"] == "Achilles"
    assert cmc["return_date"] == "2025-09-14"

    other = items[1]
    # ESPN sometimes nests status as an object rather than a string.
    assert other["status"] == "Out"
    assert other["practice_participation"] == "did_not_practice"


def test_injuries_parse_from_the_grouped_shape_too():
    items = espn.parse_injuries(fx.ESPN_INJURIES_GROUPED)
    assert [i["name"] for i in items] == ["Christian McCaffrey", "Someone Else"]


def test_unexpected_injury_shapes_degrade_instead_of_raising():
    # ESPN's API is unversioned, so anything unrecognised must yield [] not 500.
    assert espn.parse_injuries({}) == []
    assert espn.parse_injuries(None) == []
    assert espn.parse_injuries({"injuries": ["not-a-dict", 42]}) == []
    assert espn.parse_injuries({"injuries": [{"athlete": {}}]}) == []


def test_scoreboard_is_normalised_to_home_away_pairs():
    games = espn.parse_scoreboard(fx.ESPN_SCOREBOARD)
    assert len(games) == 2
    assert games[0]["home_team"] == "KC"
    assert games[0]["away_team"] == "DET"
    assert games[0]["week"] == 2
    assert games[1]["home_team"] == "MIN"
    assert espn.parse_scoreboard({}) == []


# --- Weather ------------------------------------------------------------------


def test_every_nfl_team_has_a_stadium():
    assert len(STADIUMS) == 32
    for abbr, meta in STADIUMS.items():
        assert -90 <= meta["lat"] <= 90 and -180 <= meta["lon"] <= 180, abbr
        assert isinstance(meta["indoor"], bool)


def test_team_names_and_aliases_resolve():
    assert nfl_team_abbr("Kansas City Chiefs") == "KC"
    assert nfl_team_abbr("Los Angeles Chargers") == "LAC"
    assert nfl_team_abbr("New York Jets") == "NYJ"
    assert normalise_abbr("JAC") == "JAX"
    assert normalise_abbr("OAK") == "LV"
    assert normalise_abbr("nope") is None


def test_forecast_picks_the_hour_closest_to_kickoff():
    kickoff = datetime(2025, 9, 14, 17, 0, tzinfo=timezone.utc)
    summary = weather.summarise_forecast(fx.OPEN_METEO, kickoff)
    assert summary["available"] is True
    assert summary["forecast_time"] == "2025-09-14T17:00"
    assert summary["wind_mph"] == 22.5
    assert summary["temperature_f"] == 72.0
    impact = summary["fantasy_impact"]
    # 31 mph gusts plus a 70% chance of rain is a real kicker concern.
    assert impact["severity"] == "high"
    assert impact["affects_kickers"] is True


def test_kickoff_outside_the_forecast_window_is_reported_not_guessed():
    far_off = datetime(2025, 10, 1, 17, 0, tzinfo=timezone.utc)
    summary = weather.summarise_forecast(fx.OPEN_METEO, far_off)
    assert summary["available"] is False
    assert weather.summarise_forecast({"hourly": {}}, far_off)["available"] is False


def test_calm_weather_reports_no_concern():
    calm = {
        "hourly": {
            "time": ["2025-09-14T17:00"],
            "temperature_2m": [68.0],
            "wind_speed_10m": [5.0],
            "wind_gusts_10m": [8.0],
            "precipitation_probability": [5],
            "snowfall": [0.0],
        }
    }
    impact = weather.summarise_forecast(
        calm, datetime(2025, 9, 14, 17, 0, tzinfo=timezone.utc)
    )["fantasy_impact"]
    assert impact["severity"] == "none"
    assert impact["affects_kickers"] is False


# --- Cache --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_refreshes_only_when_stale(tmp_path):
    cache = KeyedDiskCache(tmp_path / "c.json", name="t", default_ttl_seconds=3600)
    calls = []

    async def loader():
        calls.append(1)
        return {"value": len(calls)}

    first, _ = await cache.get_or_refresh("k", loader)
    second, _ = await cache.get_or_refresh("k", loader)
    assert first == second == {"value": 1}
    assert len(calls) == 1

    # A zero TTL forces a refresh.
    third, _ = await cache.get_or_refresh("k", loader, ttl=0)
    assert third == {"value": 2}


@pytest.mark.asyncio
async def test_a_failed_refresh_serves_the_stale_copy(tmp_path):
    cache = KeyedDiskCache(tmp_path / "c.json", name="t", default_ttl_seconds=0)

    async def good():
        return {"ok": True}

    async def bad():
        raise RuntimeError("upstream down")

    await cache.get_or_refresh("k", good)
    data, meta = await cache.get_or_refresh("k", bad)
    assert data == {"ok": True}
    assert "upstream down" in meta["refresh_failed"]


@pytest.mark.asyncio
async def test_a_failed_refresh_with_nothing_cached_raises(tmp_path):
    cache = KeyedDiskCache(tmp_path / "c.json", name="t", default_ttl_seconds=0)

    async def bad():
        raise RuntimeError("upstream down")

    with pytest.raises(RuntimeError):
        await cache.get_or_refresh("k", bad)


def test_cache_ignores_a_file_written_by_an_older_schema(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"schema_version": 1, "entries": {"k": {"fetched_at": 0, "data": 1}}}')
    old = KeyedDiskCache(path, name="t", default_ttl_seconds=3600, schema_version=2)
    assert old.load() is False
    assert old.peek("k") is None


# --- Snapshot integration -----------------------------------------------------


def test_include_parsing_rejects_unknown_sources():
    from fastapi import HTTPException

    assert parse_includes(None) == []
    assert parse_includes("odds, weather") == ["odds", "weather"]
    assert parse_includes("odds,odds") == ["odds"]
    with pytest.raises(HTTPException) as exc:
        parse_includes("odds,astrology")
    assert "astrology" in str(exc.value.detail)


def test_rostered_players_are_collected_across_every_slot():
    teams = [
        {
            "starters": [
                {"player": {"player_id": "1", "name": "A", "nfl_team": "KC"}},
                {"player": None},
            ],
            "bench": [{"player_id": "2", "name": "B", "nfl_team": "SF"}],
            "injured_reserve": [{"player_id": "3", "name": "C", "nfl_team": "JAC"}],
            "taxi_squad": [],
        },
        {
            "starters": [{"player": {"player_id": "1", "name": "A", "nfl_team": "KC"}}],
            "bench": [],
            "injured_reserve": [],
            "taxi_squad": [],
        },
    ]
    players = rostered_players(teams)
    assert sorted(p["player_id"] for p in players) == ["1", "2", "3"]
    # JAC is normalised to JAX so the ESPN/weather lookups hit the right team.
    assert rostered_teams(players) == ["JAX", "KC", "SF"]
