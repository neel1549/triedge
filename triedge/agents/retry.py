"""Retry worker: re-run a single flaky test and capture the outcome."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from triedge.state import RetryOutcome


def _split_test_id(test_id: str) -> tuple[str, str]:
    """Split `path::nodeid` into (file_path, nodeid). nodeid may be empty."""
    if "::" in test_id:
        file_path, nodeid = test_id.split("::", 1)
        return file_path, nodeid
    return test_id, ""


def run_retry(test_id: str, repo: str | Path) -> dict[str, Any]:
    """Re-run the single test via pytest, if available.

    Returns a dict with outcome/details. If pytest is not installed, records a
    dry-run result rather than failing.
    """
    repo_path = Path(repo)

    if shutil.which("pytest") is None:
        return {
            "outcome": RetryOutcome.DRY_RUN.value,
            "passed": None,
            "details": "pytest not found on PATH; retry not executed.",
        }

    try:
        result = subprocess.run(
            ["pytest", test_id, "-q", "--no-header"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "outcome": RetryOutcome.DRY_RUN.value,
            "passed": None,
            "details": f"pytest invocation failed: {exc}",
        }

    passed = result.returncode == 0
    tail = (result.stdout or "").splitlines()[-15:]
    return {
        "outcome": RetryOutcome.PASSED.value if passed else RetryOutcome.FAILED.value,
        "passed": passed,
        "returncode": result.returncode,
        "details": "\n".join(tail),
    }
