"""Offline tests for bye-week derivation from the nflverse schedule."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("API_KEY", "test-key")

from app.schedule import _parse_schedule  # noqa: E402

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
