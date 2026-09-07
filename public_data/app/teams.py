"""Static NFL team reference: names, abbreviations and stadium locations.

Stadium coordinates and roof type only change when a team moves or opens a new
building, so they live in code rather than behind another API call. Sleeper and
nflverse both use the abbreviations in `STADIUMS`.
"""

from __future__ import annotations

from typing import Any

# abbr -> stadium metadata. `indoor` covers domes and retractable roofs that are
# routinely closed; weather is not fetched for those.
STADIUMS: dict[str, dict[str, Any]] = {
    "ARI": {"team": "Arizona Cardinals", "stadium": "State Farm Stadium", "lat": 33.5277, "lon": -112.2626, "indoor": True, "roof": "retractable"},
    "ATL": {"team": "Atlanta Falcons", "stadium": "Mercedes-Benz Stadium", "lat": 33.7554, "lon": -84.4008, "indoor": True, "roof": "retractable"},
    "BAL": {"team": "Baltimore Ravens", "stadium": "M&T Bank Stadium", "lat": 39.2780, "lon": -76.6227, "indoor": False, "roof": "open"},
    "BUF": {"team": "Buffalo Bills", "stadium": "Highmark Stadium", "lat": 42.7738, "lon": -78.7870, "indoor": False, "roof": "open"},
    "CAR": {"team": "Carolina Panthers", "stadium": "Bank of America Stadium", "lat": 35.2258, "lon": -80.8528, "indoor": False, "roof": "open"},
    "CHI": {"team": "Chicago Bears", "stadium": "Soldier Field", "lat": 41.8623, "lon": -87.6167, "indoor": False, "roof": "open"},
    "CIN": {"team": "Cincinnati Bengals", "stadium": "Paycor Stadium", "lat": 39.0955, "lon": -84.5161, "indoor": False, "roof": "open"},
    "CLE": {"team": "Cleveland Browns", "stadium": "Huntington Bank Field", "lat": 41.5061, "lon": -81.6995, "indoor": False, "roof": "open"},
    "DAL": {"team": "Dallas Cowboys", "stadium": "AT&T Stadium", "lat": 32.7473, "lon": -97.0945, "indoor": True, "roof": "retractable"},
    "DEN": {"team": "Denver Broncos", "stadium": "Empower Field at Mile High", "lat": 39.7439, "lon": -105.0201, "indoor": False, "roof": "open"},
    "DET": {"team": "Detroit Lions", "stadium": "Ford Field", "lat": 42.3400, "lon": -83.0456, "indoor": True, "roof": "dome"},
    "GB": {"team": "Green Bay Packers", "stadium": "Lambeau Field", "lat": 44.5013, "lon": -88.0622, "indoor": False, "roof": "open"},
    "HOU": {"team": "Houston Texans", "stadium": "NRG Stadium", "lat": 29.6847, "lon": -95.4107, "indoor": True, "roof": "retractable"},
    "IND": {"team": "Indianapolis Colts", "stadium": "Lucas Oil Stadium", "lat": 39.7601, "lon": -86.1639, "indoor": True, "roof": "retractable"},
    "JAX": {"team": "Jacksonville Jaguars", "stadium": "EverBank Stadium", "lat": 30.3239, "lon": -81.6373, "indoor": False, "roof": "open"},
    "KC": {"team": "Kansas City Chiefs", "stadium": "GEHA Field at Arrowhead", "lat": 39.0489, "lon": -94.4839, "indoor": False, "roof": "open"},
    "LAC": {"team": "Los Angeles Chargers", "stadium": "SoFi Stadium", "lat": 33.9535, "lon": -118.3392, "indoor": True, "roof": "fixed-canopy"},
    "LAR": {"team": "Los Angeles Rams", "stadium": "SoFi Stadium", "lat": 33.9535, "lon": -118.3392, "indoor": True, "roof": "fixed-canopy"},
    "LV": {"team": "Las Vegas Raiders", "stadium": "Allegiant Stadium", "lat": 36.0909, "lon": -115.1833, "indoor": True, "roof": "dome"},
    "MIA": {"team": "Miami Dolphins", "stadium": "Hard Rock Stadium", "lat": 25.9580, "lon": -80.2389, "indoor": False, "roof": "open"},
    "MIN": {"team": "Minnesota Vikings", "stadium": "U.S. Bank Stadium", "lat": 44.9736, "lon": -93.2575, "indoor": True, "roof": "dome"},
    "NE": {"team": "New England Patriots", "stadium": "Gillette Stadium", "lat": 42.0909, "lon": -71.2643, "indoor": False, "roof": "open"},
    "NO": {"team": "New Orleans Saints", "stadium": "Caesars Superdome", "lat": 29.9511, "lon": -90.0812, "indoor": True, "roof": "dome"},
    "NYG": {"team": "New York Giants", "stadium": "MetLife Stadium", "lat": 40.8135, "lon": -74.0745, "indoor": False, "roof": "open"},
    "NYJ": {"team": "New York Jets", "stadium": "MetLife Stadium", "lat": 40.8135, "lon": -74.0745, "indoor": False, "roof": "open"},
    "PHI": {"team": "Philadelphia Eagles", "stadium": "Lincoln Financial Field", "lat": 39.9008, "lon": -75.1675, "indoor": False, "roof": "open"},
    "PIT": {"team": "Pittsburgh Steelers", "stadium": "Acrisure Stadium", "lat": 40.4468, "lon": -80.0158, "indoor": False, "roof": "open"},
    "SEA": {"team": "Seattle Seahawks", "stadium": "Lumen Field", "lat": 47.5952, "lon": -122.3316, "indoor": False, "roof": "open"},
    "SF": {"team": "San Francisco 49ers", "stadium": "Levi's Stadium", "lat": 37.4033, "lon": -121.9694, "indoor": False, "roof": "open"},
    "TB": {"team": "Tampa Bay Buccaneers", "stadium": "Raymond James Stadium", "lat": 27.9759, "lon": -82.5033, "indoor": False, "roof": "open"},
    "TEN": {"team": "Tennessee Titans", "stadium": "Nissan Stadium", "lat": 36.1665, "lon": -86.7713, "indoor": False, "roof": "open"},
    "WAS": {"team": "Washington Commanders", "stadium": "Northwest Stadium", "lat": 38.9076, "lon": -76.8645, "indoor": False, "roof": "open"},
}

# Full team name -> abbreviation, for sources that return display names
# (The Odds API) instead of abbreviations.
NAME_TO_ABBR: dict[str, str] = {
    meta["team"].lower(): abbr for abbr, meta in STADIUMS.items()
}
# Nickname-only fallbacks ("Chiefs", "49ers", ...).
NICKNAME_TO_ABBR: dict[str, str] = {
    meta["team"].rsplit(" ", 1)[-1].lower(): abbr for abbr, meta in STADIUMS.items()
}
# Both LA teams share a nickname pattern that resolves fine, but Giants/Jets
# and Rams/Chargers need the full name; drop the ambiguous nicknames.
for _nick in ("giants", "jets"):
    NICKNAME_TO_ABBR.pop(_nick, None)

# Abbreviations that other sources use for the same team. The GNB/KAN/NWE
# family is Pro-Football-Reference's, which is what nflverse's draft_picks and
# combine releases carry.
ABBR_ALIASES: dict[str, str] = {
    "GNB": "GB",
    "KAN": "KC",
    "LVR": "LV",
    "NOR": "NO",
    "NWE": "NE",
    "SFO": "SF",
    "TAM": "TB",
    "JAC": "JAX",
    "LA": "LAR",
    "SD": "LAC",
    "OAK": "LV",
    "STL": "LAR",
    "WSH": "WAS",
    "ARZ": "ARI",
    "BLT": "BAL",
    "CLV": "CLE",
    "HST": "HOU",
}


def normalise_abbr(abbr: str | None) -> str | None:
    if not abbr:
        return None
    upper = str(abbr).strip().upper()
    upper = ABBR_ALIASES.get(upper, upper)
    return upper if upper in STADIUMS else None


def nfl_team_abbr(name: str | None) -> str | None:
    """Resolve a team name or abbreviation to the canonical abbreviation."""
    if not name:
        return None
    text = str(name).strip()
    direct = normalise_abbr(text)
    if direct:
        return direct
    lowered = text.lower()
    if lowered in NAME_TO_ABBR:
        return NAME_TO_ABBR[lowered]
    return NICKNAME_TO_ABBR.get(lowered.rsplit(" ", 1)[-1])


def stadium_for(abbr: str | None) -> dict[str, Any] | None:
    key = normalise_abbr(abbr)
    return STADIUMS.get(key) if key else None
