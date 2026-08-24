"""Quarantine worker.

Creates a sandbox, adds an `@pytest.mark.quarantine` decorator above the failing
test, and posts a Slack (stub) alert to the test writer identified via blame.
If the test file or function can't be located, writes a `QUARANTINE_PATCH.md`
describing the intended change instead of failing the graph.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from triedge.sandbox_env import Sandbox, create_sandbox
from triedge.tools.slack import post_quarantine_alert


def _split_test_id(test_id: str) -> tuple[str, str]:
    if "::" in test_id:
        file_path, nodeid = test_id.split("::", 1)
        return file_path, nodeid
    return test_id, ""


def _target_function(nodeid: str) -> str:
    """Get the test function name from a nodeid like `TestX::test_y[param]`."""
    if not nodeid:
        return ""
    last = nodeid.split("::")[-1]
    return last.split("[")[0]


def _insert_decorator(source: str, func_name: str) -> Optional[str]:
    """Insert the quarantine decorator above `def func_name(`.

    Returns the new file text, or None if the function was not found or is
    already quarantined.
    """
    lines = source.splitlines(keepends=True)
    pattern = re.compile(rf"^(?P<indent>\s*)def {re.escape(func_name)}\s*\(")
    for idx, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        indent = match.group("indent")

        # Already quarantined just above? Look back past existing decorators.
        look = idx - 1
        while look >= 0 and lines[look].lstrip().startswith("@"):
            if "quarantine" in lines[look]:
                return None
            look -= 1

        decorator = (
            f"{indent}@pytest.mark.quarantine  "
            f"# auto-added by Triedge: failing repeatedly back-to-back\n"
        )
        lines.insert(idx, decorator)
        new_text = "".join(lines)
        if "import pytest" not in new_text:
            new_text = "import pytest\n" + new_text
        return new_text
    return None


def run_quarantine(
    *,
    test_id: str,
    repo: str | Path,
    consecutive_failures: int,
    reason: str,
    blame: dict[str, Any],
    sandbox: Optional[Sandbox] = None,
) -> dict[str, Any]:
    """Apply the quarantine marker in a sandbox and flag the writer in Slack."""
    owns_sandbox = sandbox is None
    sandbox = sandbox or create_sandbox(repo)

    file_path, nodeid = _split_test_id(test_id)
    func_name = _target_function(nodeid)
    target = sandbox.path / file_path

    patch_summary = ""
    applied = False

    if target.exists() and func_name:
        source = target.read_text()
        new_text = _insert_decorator(source, func_name)
        if new_text is not None:
            target.write_text(new_text)
            applied = True
            patch_summary = (
                f"Added @pytest.mark.quarantine to {file_path}::{func_name}."
            )
        else:
            patch_summary = (
                f"{file_path}::{func_name} already quarantined or decorator "
                f"insertion point not found."
            )

    if not applied and not (target.exists() and func_name):
        # Could not locate the test; leave a note in the sandbox.
        note = sandbox.path / "QUARANTINE_PATCH.md"
        note.write_text(
            f"# Intended quarantine\n\n"
            f"- Test: `{test_id}`\n"
            f"- Reason: {reason}\n"
            f"- Consecutive failures: {consecutive_failures}\n\n"
            f"Could not locate `{file_path}`"
            + (f"::`{func_name}`" if func_name else "")
            + " in the sandbox to add `@pytest.mark.quarantine` automatically. "
            "Please add the marker manually.\n"
        )
        patch_summary = f"Wrote {note} (could not auto-edit the test)."

    # Identify the writer via blame (top author on the failing path).
    writer = ""
    for entry in blame.get("entries", []):
        if entry.get("author"):
            writer = entry["author"]
            break

    diff = sandbox.diff()
    slack_path = post_quarantine_alert(
        repo=repo,
        test_id=test_id,
        writer=writer,
        consecutive_failures=consecutive_failures,
        reason=reason,
        diff_path=str(sandbox.path),
    )

    result = {
        "sandbox_path": str(sandbox.path),
        "patch_summary": patch_summary,
        "slack_path": slack_path,
        "applied": applied,
        "diff": diff,
    }
    if owns_sandbox:
        # Keep the sandbox on disk so the user can inspect the change; the path
        # is reported in the result.
        pass
    return result
