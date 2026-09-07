"""Append-only archive for the two sources that cannot be re-fetched later.

Everything else this service reads stays available upstream forever: nflverse
publishes a file per season going back to 1999, Sleeper keeps past seasons
reachable through `previous_league_id`, and Open-Meteo has a historical archive
API. Re-storing those would duplicate a better-maintained public archive.

Two things do evaporate:

    odds      The free Odds API tier only returns upcoming games. Once a game
              kicks off its closing line is gone (historical odds are a paid
              add-on).
    injuries  ESPN has no historical endpoint at all. Wednesday's "limited" is
              overwritten by Thursday's "full", and once the week passes there
              is no record that either happened.

Those are captured here as JSON Lines, one file per season and source, appended
to and never rewritten. A capture that would write a row identical to the last
one recorded for the same subject is skipped, so running the cron more often
than the lines move costs nothing but still records every real change.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

SOURCES = ("odds", "injuries")

# Fields compared to decide whether a row is a real change or a repeat.
CHANGE_FIELDS: dict[str, tuple[str, ...]] = {
    "odds": ("home_spread", "away_spread", "total", "favourite", "moneyline"),
    "injuries": ("status", "practice_participation", "injury_type", "return_date"),
}

# What identifies the subject of a row within a (season, week).
SUBJECT_FIELDS: dict[str, tuple[str, ...]] = {
    "odds": ("game_id",),
    "injuries": ("team", "espn_id", "name"),
}


class HistoryStore:
    """One JSON Lines file per source per season, appended to forever."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._lock = asyncio.Lock()
        # Last recorded state per subject, per (source, season, week). Built
        # from the file the first time a week is touched and updated on every
        # append, so a capture does not re-read the whole season to work out
        # what changed. With more than one worker process the memos can drift,
        # which at worst writes a duplicate row - the app runs single-worker.
        self._index: dict[tuple[str, int, int], dict[tuple, dict[str, Any]]] = {}

    def path_for(self, source: str, season: int) -> Path:
        return self.directory / f"{source}_{season}.jsonl"

    # --- Reading --------------------------------------------------------------

    def read(
        self,
        source: str,
        season: int,
        *,
        week: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self._iter_rows(self.path_for(source, season))
            if week is None or row.get("week") == week
        ]
        return rows[-limit:] if limit else rows

    @staticmethod
    def _iter_rows(path: Path) -> Iterator[dict[str, Any]]:
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line_number, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        # A torn final line from an interrupted write should not
                        # make the whole archive unreadable.
                        log.warning("Skipping malformed line %s in %s", line_number, path)
        except FileNotFoundError:
            return

    def _latest_by_subject(self, source: str, season: int, week: int) -> dict[tuple, dict[str, Any]]:
        """The most recent row recorded for each subject in this week."""
        memo_key = (source, season, week)
        cached = self._index.get(memo_key)
        if cached is not None:
            return cached

        latest: dict[tuple, dict[str, Any]] = {}
        for row in self.read(source, season, week=week):
            latest[self._subject_key(source, row)] = row
        self._index[memo_key] = latest
        return latest

    @staticmethod
    def _subject_key(source: str, row: dict[str, Any]) -> tuple:
        return tuple(row.get(field) for field in SUBJECT_FIELDS[source])

    @staticmethod
    def _changed(source: str, new: dict[str, Any], previous: dict[str, Any] | None) -> bool:
        if previous is None:
            return True
        return any(
            new.get(field) != previous.get(field) for field in CHANGE_FIELDS[source]
        )

    # --- Writing --------------------------------------------------------------

    async def append(
        self,
        source: str,
        season: int,
        week: int,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Append rows that differ from the last recorded state.

        Returns counts rather than the rows, so a cron call has something short
        to log.
        """
        if source not in SOURCES:
            raise ValueError(f"Unknown history source '{source}'.")

        async with self._lock:
            latest = self._latest_by_subject(source, season, week)
            captured_at = datetime.now(timezone.utc).isoformat()

            new_rows: list[dict[str, Any]] = []
            for row in rows:
                enriched = {
                    "captured_at": captured_at,
                    "season": season,
                    "week": week,
                    **row,
                }
                previous = latest.get(self._subject_key(source, enriched))
                if self._changed(source, enriched, previous):
                    new_rows.append(enriched)

            if not new_rows:
                return {"source": source, "written": 0, "skipped": len(rows), "unchanged": True}

            path = self.path_for(source, season)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = "".join(
                    json.dumps(row, ensure_ascii=False) + "\n" for row in new_rows
                )
                # If a previous write was interrupted the file may not end in a
                # newline; starting on a fresh line keeps that damage confined
                # to the torn line instead of also corrupting this one.
                if _needs_leading_newline(path):
                    payload = "\n" + payload
                # One O_APPEND write so a concurrent capture cannot interleave.
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(payload)
            except OSError as exc:
                log.warning("Could not append history to %s: %s", path, exc)
                return {"source": source, "written": 0, "error": str(exc)}

            for row in new_rows:
                latest[self._subject_key(source, row)] = row

            return {
                "source": source,
                "written": len(new_rows),
                "skipped": len(rows) - len(new_rows),
                "unchanged": False,
                "file": str(path),
            }

    # --- Inventory ------------------------------------------------------------

    def inventory(self) -> dict[str, Any]:
        """What is archived, per source and season."""
        summary: dict[str, Any] = {}
        for source in SOURCES:
            seasons: dict[str, Any] = {}
            for path in sorted(self.directory.glob(f"{source}_*.jsonl")):
                season = path.stem.rsplit("_", 1)[-1]
                weeks: set[int] = set()
                rows = 0
                for row in self._iter_rows(path):
                    rows += 1
                    if isinstance(row.get("week"), int):
                        weeks.add(row["week"])
                seasons[season] = {
                    "rows": rows,
                    "weeks": sorted(weeks),
                    "size_kb": round(path.stat().st_size / 1024, 1),
                }
            summary[source] = seasons
        return summary


def _needs_leading_newline(path: Path) -> bool:
    """True when the file exists, is non-empty and does not end in a newline."""
    try:
        size = path.stat().st_size
        if size == 0:
            return False
        with path.open("rb") as fh:
            fh.seek(-1, 2)
            return fh.read(1) != b"\n"
    except (OSError, ValueError):
        return False


# --- Row shaping --------------------------------------------------------------


def odds_rows(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim an odds payload to the fields worth keeping forever."""
    return [
        {
            "game_id": game.get("game_id"),
            "kickoff": game.get("kickoff"),
            "home_team": game.get("home_team_abbr") or game.get("home_team"),
            "away_team": game.get("away_team_abbr") or game.get("away_team"),
            "home_spread": game.get("home_spread"),
            "away_spread": game.get("away_spread"),
            "total": game.get("total"),
            "favourite": game.get("favourite_abbr") or game.get("favourite"),
            "spread": game.get("spread"),
            "moneyline": game.get("moneyline"),
            "implied_team_totals": game.get("implied_team_totals"),
            "bookmakers_counted": game.get("bookmakers_counted"),
        }
        for game in games
        if game.get("game_id")
    ]


def injury_rows(team: str, injuries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim an ESPN team report to the fields worth keeping forever."""
    return [
        {
            "team": team,
            "espn_id": item.get("espn_id"),
            "name": item.get("name"),
            "position": item.get("position"),
            "status": item.get("status"),
            "practice_participation": item.get("practice_participation"),
            "injury_type": item.get("injury_type"),
            "return_date": item.get("return_date"),
            "espn_updated": item.get("updated"),
        }
        for item in injuries
        if item.get("name") or item.get("espn_id")
    ]


# --- Capture ------------------------------------------------------------------

# ESPN is free and unmetered, but 32 simultaneous requests is impolite.
ESPN_CONCURRENCY = 6


async def capture_week(
    store: HistoryStore,
    *,
    odds_provider: Any,
    espn_provider: Any,
    season: int,
    week: int,
    teams: list[str],
    refresh: bool = True,
) -> dict[str, Any]:
    """Archive this week's betting lines and injury reports.

    `refresh=True` bypasses the read caches so the archived line is the one
    live at capture time rather than whatever a browsing request happened to
    warm the cache with hours earlier. That is the whole point of capturing on
    a schedule, so it defaults on.
    """
    odds_result, injuries_result = await asyncio.gather(
        _capture_odds(store, odds_provider, season, week, refresh),
        _capture_injuries(store, espn_provider, season, week, teams, refresh),
    )
    return {
        "season": season,
        "week": week,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "odds": odds_result,
        "injuries": injuries_result,
    }


async def _capture_odds(
    store: HistoryStore, provider: Any, season: int, week: int, refresh: bool
) -> dict[str, Any]:
    if not provider.configured:
        return {
            "written": 0,
            "skipped": 0,
            "error": "ODDS_API_KEY is not configured; nothing to archive.",
        }
    try:
        if refresh:
            # ttl=0 forces one live call, which is what makes this a real
            # closing-line snapshot instead of a copy of the cache.
            data, _ = await provider.cache.get_or_refresh(
                f"week:{season}:{week}", provider._fetch, ttl=0
            )
            games = data.get("games", [])
        else:
            games = (await provider.for_week(week, season)).get("games", [])
    except Exception as exc:  # noqa: BLE001 - a failed source must not abort the rest
        log.warning("Odds capture failed: %s", exc)
        return {"written": 0, "skipped": 0, "error": str(getattr(exc, "detail", exc))}

    result = await store.append("odds", season, week, odds_rows(games))
    result["games_seen"] = len(games)
    return result


async def _capture_injuries(
    store: HistoryStore,
    provider: Any,
    season: int,
    week: int,
    teams: list[str],
    refresh: bool,
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(ESPN_CONCURRENCY)

    async def one(team: str) -> tuple[str, Any]:
        async with semaphore:
            try:
                if refresh:
                    data, _ = await provider.cache.get_or_refresh(
                        f"team:{team}", lambda: provider._fetch_team(team), ttl=0
                    )
                    return team, data.get("injuries", [])
                report = await provider.team_report(team)
                return team, report.get("injuries", [])
            except Exception as exc:  # noqa: BLE001
                return team, exc

    results = await asyncio.gather(*[one(team) for team in teams])

    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for team, outcome in results:
        if isinstance(outcome, BaseException):
            failures.append(f"{team}: {getattr(outcome, 'detail', outcome)}")
            continue
        rows.extend(injury_rows(team, outcome))

    result = await store.append("injuries", season, week, rows)
    result["teams_captured"] = len(teams) - len(failures)
    if failures:
        result["teams_failed"] = failures
    return result


# --- Opportunistic capture ----------------------------------------------------


async def auto_capture(
    store: HistoryStore,
    source: str,
    season: int | None,
    week: int | None,
    rows: list[dict[str, Any]],
) -> None:
    """Archive rows that a read endpoint just pulled from upstream.

    Best-effort by design: this runs on the path of an ordinary read request,
    so a broken archive must never turn a working `/odds` call into a 500. Any
    failure is logged and swallowed.

    Only call this when the data actually came from upstream. Archiving a cache
    hit would re-scan the archive just to conclude nothing changed.
    """
    if not rows or season is None or week is None:
        return
    try:
        result = await store.append(source, season, week, rows)
        if result.get("written"):
            log.info(
                "Auto-captured %s row(s) of %s for %s week %s.",
                result["written"],
                source,
                season,
                week,
            )
    except Exception as exc:  # noqa: BLE001 - never fail the read it rode in on
        log.warning("Auto-capture of %s failed: %s", source, exc)
