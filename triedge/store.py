"""In-memory failure history store, with optional JSON persistence.

The store is the cross-run "ephemeral memory" the router consults: how many
times a test has failed back-to-back, the summarized traces, and the decisions
Triedge made previously along with their outcomes.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class FailureRecord:
    """A single observed failure and what Triedge did about it."""

    timestamp: float
    trace_summary: str
    decision: str = ""
    outcome: str = ""  # e.g. validation result / retry result
    passed: Optional[bool] = None  # True if the test later passed on retry

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FailureRecord":
        return cls(
            timestamp=data.get("timestamp", 0.0),
            trace_summary=data.get("trace_summary", ""),
            decision=data.get("decision", ""),
            outcome=data.get("outcome", ""),
            passed=data.get("passed"),
        )


@dataclass
class TestHistory:
    """All recorded history for one test id."""

    test_id: str
    records: list[FailureRecord] = field(default_factory=list)

    @property
    def total_failures(self) -> int:
        return len(self.records)

    @property
    def consecutive_failures(self) -> int:
        """Length of the current back-to-back failure streak.

        A recorded `passed is True` breaks the streak. Records where the test
        was never re-run (passed is None) still count as failures, since the
        record only exists because the test failed.
        """
        streak = 0
        for record in reversed(self.records):
            if record.passed is True:
                break
            streak += 1
        return streak

    @property
    def looks_flaky(self) -> bool:
        """Heuristic: has this test both passed and failed recently?"""
        recent = self.records[-6:]
        saw_pass = any(r.passed is True for r in recent)
        saw_fail = any(r.passed is not True for r in recent)
        return saw_pass and saw_fail and len(recent) >= 2

    @property
    def recent_summaries(self) -> list[str]:
        return [r.trace_summary for r in self.records[-5:] if r.trace_summary]

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "records": [r.to_dict() for r in self.records],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TestHistory":
        return cls(
            test_id=data.get("test_id", ""),
            records=[FailureRecord.from_dict(r) for r in data.get("records", [])],
        )

    def summary(self) -> dict[str, Any]:
        """Compact view handed to the router."""
        return {
            "test_id": self.test_id,
            "total_failures": self.total_failures,
            "consecutive_failures": self.consecutive_failures,
            "looks_flaky": self.looks_flaky,
            "recent_summaries": self.recent_summaries,
            "recent_decisions": [r.decision for r in self.records[-5:] if r.decision],
        }


class InMemoryStore:
    """Maps `test_id -> TestHistory`, optionally backed by a JSON file."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._data: dict[str, TestHistory] = {}
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            self.load()

    def load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        for test_id, hist in raw.items():
            self._data[test_id] = TestHistory.from_dict(hist)

    def save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        serialized = {tid: hist.to_dict() for tid, hist in self._data.items()}
        self._path.write_text(json.dumps(serialized, indent=2))

    def get(self, test_id: str) -> TestHistory:
        return self._data.setdefault(test_id, TestHistory(test_id=test_id))

    def summary(self, test_id: str) -> dict[str, Any]:
        return self.get(test_id).summary()

    def record_failure(
        self,
        test_id: str,
        trace_summary: str,
        decision: str = "",
        outcome: str = "",
        passed: Optional[bool] = None,
    ) -> FailureRecord:
        record = FailureRecord(
            timestamp=time.time(),
            trace_summary=trace_summary,
            decision=decision,
            outcome=outcome,
            passed=passed,
        )
        self.get(test_id).records.append(record)
        return record

    def all(self) -> dict[str, TestHistory]:
        return dict(self._data)
