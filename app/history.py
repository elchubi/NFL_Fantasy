"""The decision log.

Everything a manager decides, and why, at the moment they decide it - before
they know how it turned out. Two seasons of these, read against what actually
happened, is the only way to find out where your process is systematically
wrong. No public tool can tell you, because none of them knows what you
decided or why.

Betting lines and injury reports used to be captured here too, but they are
not specific to any one league, so that logic (and the odds_history /
injury_history tables) moved to the shared public-data service. This module
now only concerns the decision log, which stays per-league.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

# The public-data service archives "odds" and "injuries"; this league backend
# only stores "decisions" locally. Kept as one tuple so /history/{source}
# validates against the same three names regardless of where each is served
# from - see main.py's proxying for "odds"/"injuries".
SOURCES = ("odds", "injuries", "decisions")

DECISION_KINDS = (
    "waiver_bid", "trade", "start_sit", "draft_pick", "keeper", "drop", "other",
)


def decision_row(
    *,
    kind: str,
    summary: str,
    reasoning: str | None = None,
    players: list[str] | None = None,
    confidence: str | None = None,
    expected: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    """One entry in the decision log."""
    return {
        "decision_id": decision_id or uuid.uuid4().hex[:12],
        "kind": kind if kind in DECISION_KINDS else "other",
        "kind_raw": kind,
        "summary": summary,
        "reasoning": reasoning,
        "players": players or [],
        "confidence": confidence,
        "expected": expected,
        "outcome": None,
        "outcome_recorded_at": None,
    }


def outcome_row(original: dict[str, Any], outcome: str) -> dict[str, Any]:
    """The follow-up entry recording how a decision turned out.

    Appended rather than edited: the original call is preserved exactly as it
    was made, which is the part that matters when checking your own reasoning
    against what happened.
    """
    return {
        **{k: v for k, v in original.items() if not k.startswith("_")},
        "outcome": outcome,
        "outcome_recorded_at": datetime.now(timezone.utc).isoformat(),
    }
