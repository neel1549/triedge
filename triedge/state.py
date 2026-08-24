"""Typed state shared across the Triedge LangGraph workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, TypedDict


class Decision(str, Enum):
    """The three routes the classifier can choose."""

    SANDBOX_FIX = "sandbox_fix"
    QUARANTINE = "quarantine"
    RETRY = "retry"


class Validation(str, Enum):
    """Outcome of the sandbox fixer/validator loop."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MAX_TURNS_EXCEEDED = "max_turns_exceeded"
    SKIPPED = "skipped"  # no API key / dry-run


class RetryOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    DRY_RUN = "dry_run"


# Default consecutive-failure streak that triggers quarantine, clamped to 10-20.
DEFAULT_QUARANTINE_THRESHOLD = 10
QUARANTINE_THRESHOLD_MIN = 10
QUARANTINE_THRESHOLD_MAX = 20

DEFAULT_MAX_TURNS = 8


def clamp_quarantine_threshold(value: int) -> int:
    return max(QUARANTINE_THRESHOLD_MIN, min(QUARANTINE_THRESHOLD_MAX, value))


@dataclass
class BlameEntry:
    """A single line's blame result from the failing code path."""

    file: str
    line: int
    commit: str = ""
    author: str = ""
    author_email: str = ""
    summary: str = ""
    age_days: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "commit": self.commit,
            "author": self.author,
            "author_email": self.author_email,
            "summary": self.summary,
            "age_days": self.age_days,
        }


@dataclass
class BlameResult:
    entries: list[BlameEntry] = field(default_factory=list)
    available: bool = False

    @property
    def top_author(self) -> str:
        for entry in self.entries:
            if entry.author:
                return entry.author
        return ""

    @property
    def top_author_email(self) -> str:
        for entry in self.entries:
            if entry.author_email:
                return entry.author_email
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "entries": [e.to_dict() for e in self.entries],
        }


@dataclass
class RiskySignal:
    """Lightweight 'risky PR touched this path' heuristic result."""

    is_risky: bool = False
    reasons: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_risky": self.is_risky,
            "reasons": self.reasons,
            "commits": self.commits,
        }


class TriageState(TypedDict, total=False):
    """LangGraph state for a single triage run."""

    # Inputs
    test_id: str
    traceback: str
    repo: str
    max_turns: int
    quarantine_threshold: int

    # Derived context
    trace_summary: str
    history: dict[str, Any]

    # Signals
    blame: dict[str, Any]
    risky_signal: dict[str, Any]

    # Routing result
    decision: str
    rationale: str

    # Worker outputs
    sandbox_path: str
    patch_summary: str
    slack_path: str
    retry_result: dict[str, Any]
    validation: str
    validation_report: str
