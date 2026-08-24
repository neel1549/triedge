"""Sandbox creation + diffing.

For a git repo we create a detached worktree so the fixer/validator agents edit
an isolated checkout. For a non-git directory we fall back to a plain filesystem
copy. Either way, callers get a path they can freely edit, plus a way to diff
against the baseline and to clean up.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


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


@dataclass
class Sandbox:
    """An isolated working copy of the repo for the sandbox agents."""

    path: Path
    is_git: bool
    source: Path

    def diff(self) -> str:
        """Return a unified diff of edits made inside the sandbox."""
        if self.is_git:
            try:
                # Include tracked changes and newly added files.
                subprocess.run(
                    ["git", "-C", str(self.path), "add", "-A"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                result = subprocess.run(
                    ["git", "-C", str(self.path), "diff", "--cached"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    return result.stdout
            except (OSError, subprocess.SubprocessError):
                pass
        return "(diff unavailable for non-git sandbox)"

    def file_changed(self, rel_path: str) -> bool:
        """Whether a given file changed vs the baseline (git sandboxes only)."""
        if not self.is_git:
            return False
        try:
            result = subprocess.run(
                ["git", "-C", str(self.path), "status", "--porcelain", "--", rel_path],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return bool(result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            return False

    def cleanup(self) -> None:
        if self.is_git:
            try:
                subprocess.run(
                    ["git", "-C", str(self.source), "worktree", "remove", "--force", str(self.path)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                return
            except (OSError, subprocess.SubprocessError):
                pass
        shutil.rmtree(self.path, ignore_errors=True)


def create_sandbox(repo: str | Path) -> Sandbox:
    """Create an isolated sandbox copy of `repo`."""
    source = Path(repo).resolve()
    suffix = time.strftime("%Y%m%d-%H%M%S")
    dest = Path(tempfile.mkdtemp(prefix=f"triedge-sandbox-{suffix}-"))

    if _is_git_repo(source):
        try:
            result = subprocess.run(
                ["git", "-C", str(source), "worktree", "add", "--detach", str(dest)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                return Sandbox(path=dest, is_git=True, source=source)
        except (OSError, subprocess.SubprocessError):
            pass

    # Fallback: plain copy (skip .git and common heavy dirs).
    shutil.rmtree(dest, ignore_errors=True)
    ignore = shutil.ignore_patterns(".git", "__pycache__", ".venv", "node_modules")
    shutil.copytree(source, dest, ignore=ignore)
    return Sandbox(path=dest, is_git=False, source=source)
