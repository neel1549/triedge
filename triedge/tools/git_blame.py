"""Git blame + risky-PR signals derived from a failure traceback.

When the repo is a real git repository and the failing files exist, we run
`git blame` on the frames mentioned in the traceback. Otherwise we return an
empty (but well-formed) result so the rest of the pipeline still works.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from triedge.state import BlameEntry, BlameResult, RiskySignal

# Matches CPython traceback frame lines:
#   File "path/to/file.py", line 42, in func
_FRAME_RE = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)')

# How recent a commit must be (days) to be considered "risky" on its own.
RISKY_AGE_DAYS = 14


def _is_git_repo(repo: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def parse_frames(traceback: str, repo: Path) -> list[tuple[str, int]]:
    """Extract (relative_path, line) frames from a traceback.

    Only frames whose file exists inside the repo are kept, most-recent (the
    bottom of the traceback) first, deduplicated by file.
    """
    frames: list[tuple[str, int]] = []
    for match in _FRAME_RE.finditer(traceback):
        raw_path = match.group("file")
        line = int(match.group("line"))
        path = Path(raw_path)
        candidate = path if path.is_absolute() else (repo / path)
        if candidate.exists():
            try:
                rel = candidate.resolve().relative_to(repo.resolve())
            except ValueError:
                continue
            frames.append((str(rel), line))

    # Deepest frames (bottom of the traceback) are most relevant; keep them
    # first while deduplicating on file path.
    frames.reverse()
    seen: set[str] = set()
    deduped: list[tuple[str, int]] = []
    for file, line in frames:
        if file in seen:
            continue
        seen.add(file)
        deduped.append((file, line))
    return deduped


def _blame_line(repo: Path, file: str, line: int) -> Optional[BlameEntry]:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "blame",
                "-L",
                f"{line},{line}",
                "--line-porcelain",
                "--",
                file,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None

    lines = result.stdout.splitlines()
    commit = lines[0].split(" ")[0] if lines else ""
    author = ""
    author_email = ""
    summary = ""
    author_time: Optional[int] = None
    for raw in lines[1:]:
        if raw.startswith("author "):
            author = raw[len("author ") :].strip()
        elif raw.startswith("author-mail "):
            author_email = raw[len("author-mail ") :].strip().strip("<>")
        elif raw.startswith("author-time "):
            try:
                author_time = int(raw[len("author-time ") :].strip())
            except ValueError:
                author_time = None
        elif raw.startswith("summary "):
            summary = raw[len("summary ") :].strip()

    age_days: Optional[float] = None
    if author_time is not None:
        age_days = max(0.0, (time.time() - author_time) / 86400.0)

    return BlameEntry(
        file=file,
        line=line,
        commit=commit,
        author=author,
        author_email=author_email,
        summary=summary,
        age_days=age_days,
    )


def blame_traceback(traceback: str, repo: str | Path) -> BlameResult:
    """Run git blame on the frames referenced in the traceback."""
    repo_path = Path(repo)
    if not _is_git_repo(repo_path):
        return BlameResult(entries=[], available=False)

    entries: list[BlameEntry] = []
    for file, line in parse_frames(traceback, repo_path):
        entry = _blame_line(repo_path, file, line)
        if entry is not None:
            entries.append(entry)
    return BlameResult(entries=entries, available=True)


def _load_risky_fixture(repo: Path) -> set[str]:
    """Load optional `.triedge/risky_prs.json` listing risky commit shas."""
    fixture = repo / ".triedge" / "risky_prs.json"
    if not fixture.exists():
        return set()
    try:
        data = json.loads(fixture.read_text())
    except (json.JSONDecodeError, OSError):
        return set()

    risky: set[str] = set()
    # Accept either a list of shas, or a list of {"commit": ...} / {"sha": ...}.
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                risky.add(item)
            elif isinstance(item, dict):
                for key in ("commit", "sha", "pr", "id"):
                    if key in item:
                        risky.add(str(item[key]))
    elif isinstance(data, dict):
        for key in ("commits", "shas", "risky"):
            for item in data.get(key, []):
                risky.add(str(item))
    return risky


def assess_risk(blame: BlameResult, repo: str | Path) -> RiskySignal:
    """Decide whether a risky PR likely touched the failing code path."""
    repo_path = Path(repo)
    signal = RiskySignal()
    if not blame.available or not blame.entries:
        return signal

    risky_fixture = _load_risky_fixture(repo_path)

    for entry in blame.entries:
        short_sha = entry.commit[:8]
        matches_fixture = any(
            entry.commit.startswith(sha) or sha.startswith(short_sha)
            for sha in risky_fixture
            if sha
        )
        if matches_fixture:
            signal.is_risky = True
            signal.commits.append(short_sha)
            signal.reasons.append(
                f"{entry.file}:{entry.line} last changed by flagged commit {short_sha}"
            )
            continue
        if entry.age_days is not None and entry.age_days <= RISKY_AGE_DAYS:
            signal.is_risky = True
            signal.commits.append(short_sha)
            signal.reasons.append(
                f"{entry.file}:{entry.line} changed {entry.age_days:.1f} days ago "
                f"in {short_sha} ({entry.summary or 'no summary'})"
            )

    # Deduplicate commits while preserving order.
    seen: set[str] = set()
    signal.commits = [c for c in signal.commits if not (c in seen or seen.add(c))]
    return signal
