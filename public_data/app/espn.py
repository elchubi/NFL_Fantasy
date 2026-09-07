"""Injury reports from ESPN's unofficial site API.

ESPN publishes practice participation (full / limited / did not practice) in
the injury item's comment text, which is often fresher and more granular than
the `injury_status` baked into Sleeper's once-a-day player file.

This API is not documented or versioned by ESPN, so the parsing here is
deliberately defensive: it looks for the fields in several plausible places,
keeps the raw item around, and degrades to `null` fields rather than raising
when the shape moves. Cached for a short TTL (3h) because injury news moves
throughout the week.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from fastapi import HTTPException

from app.cache import KeyedDiskCache
from app.config import get_settings
from app.http import request_json
from app.teams import STADIUMS, normalise_abbr

log = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = 1

# ESPN uses a handful of abbreviations that differ from Sleeper's.
ESPN_ABBR_OVERRIDES = {"WAS": "wsh", "LAR": "lar", "LAC": "lac", "JAX": "jax", "LV": "lv"}

PRACTICE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("did_not_practice", re.compile(r"\b(did ?not practice|dnp|no practice)\b", re.I)),
    ("limited", re.compile(r"\blimited (?:participant|practice|in practice)?\b", re.I)),
    ("full", re.compile(r"\bfull (?:participant|practice|go)\b", re.I)),
)

# ESPN's site API is unofficial and rejects every request from this app's
# Railway deployment with a 403 (Sleeper, nflverse, The Odds API and
# Open-Meteo have all been fine). A realistic browser User-Agent was tried
# first and did not help - confirmed live, the block persisted identically
# after that fix shipped - so this is most likely an IP-range block on
# Railway's outbound traffic rather than anything about the request itself.
# Kept anyway since it can't hurt and might matter for some endpoints; the
# real fix is `allow_403` below, which lets these calls degrade to
# `source_available: false` instead of taking the whole endpoint down.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

STATUS_NORMALISATION = {
    "out": "Out",
    "doubtful": "Doubtful",
    "questionable": "Questionable",
    "probable": "Probable",
    "active": "Active",
    "injured reserve": "IR",
    "ir": "IR",
    "physically-unable-to-perform": "PUP",
    "pup": "PUP",
    "suspension": "Suspended",
    "day-to-day": "Day-To-Day",
}


class EspnProvider:
    def __init__(self, client: httpx.AsyncClient) -> None:
        settings = get_settings()
        self._client = client
        self._settings = settings
        self._base_url = settings.espn_base_url.rstrip("/")
        self.cache = KeyedDiskCache(
            settings.cache_path("injuries_cache.json"),
            name="espn",
            default_ttl_seconds=settings.espn_cache_ttl_hours * 3600,
            schema_version=CACHE_SCHEMA_VERSION,
        )

    async def team_report(self, team_abbr: str) -> dict[str, Any]:
        """Every listed injury for one team."""
        abbr = normalise_abbr(team_abbr)
        if not abbr:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"'{team_abbr}' is not an NFL team abbreviation. "
                    f"Expected one of: {', '.join(sorted(STADIUMS))}."
                ),
            )
        data, meta = await self.cache.get_or_refresh(
            f"team:{abbr}", lambda: self._fetch_team(abbr)
        )
        return {"team": abbr, **data, "cache": meta}

    async def schedule(
        self, week: int, season: int | None = None, season_type: int = 2
    ) -> dict[str, Any]:
        """The week's games, used to know which stadiums to fetch weather for."""
        key = f"schedule:{season or 'current'}:{season_type}:{week}"
        data, meta = await self.cache.get_or_refresh(
            key,
            lambda: self._fetch_schedule(week, season, season_type),
            # The schedule is stable once published; no need for the 3h injury TTL.
            ttl=24 * 3600,
        )
        return {"week": week, **data, "cache": meta}

    async def _fetch_schedule(
        self, week: int, season: int | None, season_type: int
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"week": week, "seasontype": season_type}
        if season:
            params["dates"] = season
        payload = await request_json(
            self._client,
            f"{self._base_url}/scoreboard",
            source="ESPN",
            params=params,
            headers=_BROWSER_HEADERS,
            timeout=self._settings.http_timeout,
            max_retries=self._settings.http_max_retries,
            allow_404=True,
            allow_403=True,
        )
        if payload is None:
            return {"games": [], "source_available": False}
        return {"games": parse_scoreboard(payload), "source_available": True}

    async def _fetch_team(self, abbr: str) -> dict[str, Any]:
        slug = ESPN_ABBR_OVERRIDES.get(abbr, abbr.lower())
        payload = await request_json(
            self._client,
            f"{self._base_url}/teams/{slug}/injuries",
            source="ESPN",
            headers=_BROWSER_HEADERS,
            timeout=self._settings.http_timeout,
            max_retries=self._settings.http_max_retries,
            allow_404=True,
            allow_403=True,
        )
        if payload is None:
            return {"injuries": [], "source_available": False}
        return {
            "injuries": parse_injuries(payload),
            "source_available": True,
        }


def parse_injuries(payload: Any) -> list[dict[str, Any]]:
    """Pull injury items out of whatever envelope ESPN wrapped them in.

    Observed shapes put the list under `injuries` (sometimes as
    `[{"injuries": [...]}]` grouped by team), and item fields move between the
    item itself and a nested `details`/`athlete` object, so every field is
    looked up in several places.
    """
    items: list[dict[str, Any]] = []
    for group in _candidate_lists(payload):
        for item in group:
            if isinstance(item, dict):
                parsed = parse_injury_item(item)
                if parsed:
                    items.append(parsed)
    return items


def _candidate_lists(payload: Any) -> list[list[Any]]:
    """Every list that plausibly holds injury items."""
    found: list[list[Any]] = []
    if isinstance(payload, list):
        # Either a list of injuries, or a list of per-team groups.
        if payload and isinstance(payload[0], dict) and "injuries" in payload[0]:
            for group in payload:
                if isinstance(group.get("injuries"), list):
                    found.append(group["injuries"])
        else:
            found.append(payload)
    elif isinstance(payload, dict):
        for key in ("injuries", "items", "entries"):
            value = payload.get(key)
            if isinstance(value, list):
                if value and isinstance(value[0], dict) and isinstance(
                    value[0].get("injuries"), list
                ):
                    found.extend(g["injuries"] for g in value if isinstance(g.get("injuries"), list))
                else:
                    found.append(value)
    return found


def parse_injury_item(item: dict[str, Any]) -> dict[str, Any] | None:
    athlete = item.get("athlete") or item.get("player") or {}
    if not isinstance(athlete, dict):
        athlete = {}
    details = item.get("details") if isinstance(item.get("details"), dict) else {}

    name = (
        athlete.get("displayName")
        or athlete.get("fullName")
        or athlete.get("name")
        or item.get("displayName")
    )
    espn_id = athlete.get("id") or item.get("athleteId") or item.get("id")

    comment = " ".join(
        str(part)
        for part in (
            item.get("longComment"),
            item.get("shortComment"),
            item.get("comment"),
            details.get("detail"),
        )
        if part
    ).strip()

    status_raw = item.get("status") or item.get("injuryStatus")
    if isinstance(status_raw, dict):
        status_raw = status_raw.get("name") or status_raw.get("description")
    if not status_raw:
        fantasy_status = details.get("fantasyStatus")
        if isinstance(fantasy_status, dict):
            status_raw = fantasy_status.get("description") or fantasy_status.get("abbreviation")

    position = None
    espn_position = athlete.get("position")
    if isinstance(espn_position, dict):
        position = espn_position.get("abbreviation") or espn_position.get("name")
    elif isinstance(espn_position, str):
        position = espn_position

    if not name and not espn_id:
        return None

    return {
        "espn_id": str(espn_id) if espn_id is not None else None,
        "name": name,
        "position": position,
        "status": normalise_status(status_raw),
        "status_raw": status_raw,
        "practice_participation": practice_participation(comment),
        "injury_type": details.get("type") or item.get("type"),
        "side": details.get("side"),
        "return_date": details.get("returnDate") or item.get("returnDate"),
        "updated": item.get("date") or item.get("lastModified"),
        "comment": comment or None,
    }


def normalise_status(status: Any) -> str | None:
    if not status:
        return None
    key = str(status).strip().lower()
    return STATUS_NORMALISATION.get(key, str(status).strip())


def practice_participation(comment: str | None) -> str | None:
    """Extract full / limited / did-not-practice from free-text injury notes."""
    if not comment:
        return None
    for label, pattern in PRACTICE_PATTERNS:
        if pattern.search(comment):
            return label
    return None


# --- Schedule -----------------------------------------------------------------


def parse_scoreboard(payload: Any) -> list[dict[str, Any]]:
    """Normalise ESPN's scoreboard into `[{home, away, kickoff, venue}]`.

    Same caveat as the injury parsing: the shape is unversioned, so each field
    is looked for in more than one place and a missing one yields None instead
    of an exception.
    """
    events = (payload or {}).get("events")
    if not isinstance(events, list):
        return []

    games: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        competitions = event.get("competitions") or []
        competition = competitions[0] if competitions and isinstance(competitions[0], dict) else {}
        home = away = None
        for competitor in competition.get("competitors") or []:
            if not isinstance(competitor, dict):
                continue
            team = competitor.get("team") or {}
            abbr = normalise_abbr(team.get("abbreviation")) or normalise_abbr(
                team.get("displayName")
            )
            if competitor.get("homeAway") == "home":
                home = abbr
            elif competitor.get("homeAway") == "away":
                away = abbr

        venue = competition.get("venue") or {}
        week = event.get("week") or {}
        games.append(
            {
                "event_id": event.get("id"),
                "name": event.get("shortName") or event.get("name"),
                "kickoff": event.get("date") or competition.get("date"),
                "week": week.get("number") if isinstance(week, dict) else None,
                "home_team": home,
                "away_team": away,
                "venue": venue.get("fullName"),
                "venue_indoor": venue.get("indoor"),
                "status": ((event.get("status") or {}).get("type") or {}).get("description"),
            }
        )
    return [g for g in games if g["home_team"] or g["away_team"]]
