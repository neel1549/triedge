"""Slack stub: writes a markdown 'message' to `.triedge/slack/` instead of

calling any Slack API. This flags the test writer when a test is quarantined.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return slug or "message"


def post_quarantine_alert(
    repo: str | Path,
    test_id: str,
    writer: str,
    consecutive_failures: int,
    reason: str,
    diff_path: Optional[str] = None,
    channel: str = "#flaky-tests",
) -> str:
    """Write a Slack-style markdown message and return the file path."""
    repo_path = Path(repo)
    outbox = repo_path / ".triedge" / "slack"
    outbox.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{ts}_{_slugify(test_id)}.md"
    path = outbox / filename

    writer_display = writer or "unknown (no blame author found)"
    lines = [
        f"# Quarantine alert: `{test_id}`",
        "",
        f"- Channel: {channel}",
        f"- Test writer / owner: {writer_display}",
        f"- Consecutive failures: {consecutive_failures}",
        f"- Reason: {reason}",
    ]
    if diff_path:
        lines.append(f"- Sandbox diff: `{diff_path}`")
    lines.extend(
        [
            "",
            "This test was automatically quarantined by Triedge after failing "
            "repeatedly back-to-back. Please investigate and remove the "
            "`@pytest.mark.quarantine` marker once fixed.",
            "",
        ]
    )
    path.write_text("\n".join(lines))
    return str(path)
