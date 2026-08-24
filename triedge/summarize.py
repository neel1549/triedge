"""Traceback summarization: first error line + the top frames."""

from __future__ import annotations

import re

_FRAME_RE = re.compile(r'\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+)')
# Last non-empty, non-frame line is usually "ExceptionType: message".
_ERROR_RE = re.compile(r"^(?P<exc>[A-Za-z_][\w.]*(Error|Exception|Warning|Failure))\b")


def summarize_traceback(traceback: str, max_frames: int = 3) -> str:
    """Produce a compact one-to-few line summary of a traceback."""
    if not traceback or not traceback.strip():
        return "(no traceback provided)"

    lines = [ln.rstrip() for ln in traceback.splitlines() if ln.strip()]

    # Error line: prefer the last line matching an Exception-like pattern,
    # otherwise just the last line.
    error_line = ""
    for line in reversed(lines):
        if _ERROR_RE.match(line.strip()) or "Error" in line or "assert" in line.lower():
            error_line = line.strip()
            break
    if not error_line and lines:
        error_line = lines[-1].strip()

    # Top frames (deepest last in the traceback -> most relevant).
    frames: list[str] = []
    for match in _FRAME_RE.finditer(traceback):
        frames.append(
            f"{match.group('file')}:{match.group('line')} in {match.group('func')}"
        )
    top_frames = frames[-max_frames:]

    parts = [f"error: {error_line}" if error_line else "error: (unknown)"]
    if top_frames:
        parts.append("frames: " + " <- ".join(reversed(top_frames)))
    return " | ".join(parts)
