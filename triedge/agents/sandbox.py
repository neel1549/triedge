"""Sandbox multi-agent loop: a fixer proposes a product-code fix, a validator

accepts or rejects it. The loop is bounded by `max_turns` (mapped onto Deep
Agents' ModelCallLimitMiddleware run_limit) plus a hard LangGraph recursion
ceiling, so it can never loop forever.

Two hard rules the validator enforces:
  1. No test mutation - the failing test file / helpers stay unchanged.
  2. No hardcoded pass - the production change must be a real fix, not a cheat.

When no API key is configured (or Deep Agents isn't importable), the live loop
is skipped and a dry-run VALIDATION.md is written instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from triedge.llm import available_provider, default_model_name
from triedge.sandbox_env import Sandbox, create_sandbox
from triedge.state import Validation


class SandboxResult(BaseModel):
    """Structured final outcome of the fixer/validator loop."""

    validation: str = Field(
        description="One of: accepted, rejected, max_turns_exceeded."
    )
    report: str = Field(description="Validator's reasoning with file/line detail.")
    patch_summary: str = Field(description="Short summary of the applied change.")


def _split_test_id(test_id: str) -> tuple[str, str]:
    if "::" in test_id:
        file_path, nodeid = test_id.split("::", 1)
        return file_path, nodeid
    return test_id, ""


FIXER_SYSTEM_PROMPT = """You are the FIXER agent. Your job is to make a failing \
unit test pass by fixing the PRODUCT / BUSINESS code - never by weakening the \
test or faking the result.

Hard rules (violating any of these makes your fix invalid):
1. Do NOT modify the failing test file, sibling test files, or conftest/test \
helpers. Only edit non-test production code.
2. Do NOT hardcode values or stub logic just to satisfy the assertion. \
Forbidden examples: returning a magic constant that matches the expected value, \
`return True`, swallowing/catching exceptions to hide them, or copying the \
test's expected value into production code.

Investigate the traceback, read the relevant product code, and make the \
smallest correct change that addresses the real defect. Explain what you \
changed and why."""

VALIDATOR_SYSTEM_PROMPT = """You are the VALIDATOR agent. You review the fixer's \
changes in the sandbox and decide accept or reject.

You MUST explicitly check both hard rules and cite concrete files/lines:
1. NO TEST MUTATION: the failing test file and any test helpers/conftest are \
unchanged versus the baseline. If any test file changed, REJECT.
2. NO HARDCODED PASS: the production change is a genuine fix, not a cheat to \
satisfy the assertion (no magic constants matching the expected value, no \
`return True`, no swallowed exceptions, no copying expected values into code). \
If the change is a cheat, REJECT.

Re-run the failing test to confirm behavior. Then report accepted or rejected \
with specific reasons. If you reject, give the fixer actionable feedback."""


def _parent_prompt(test_id: str, file_path: str, traceback: str, max_turns: int) -> str:
    return (
        "A unit test is failing and the router decided a code fix is plausible.\n\n"
        f"Failing test: {test_id}\n"
        f"Test file (do NOT modify): {file_path}\n\n"
        f"Traceback:\n{traceback}\n\n"
        "Orchestrate a fixer/validator loop:\n"
        "1. Delegate to the `fixer` subagent to fix the product code.\n"
        "2. Delegate to the `validator` subagent to check the two hard rules "
        "(no test mutation, no hardcoded pass) and re-run the test.\n"
        "3. If the validator rejects and you still have budget, delegate back to "
        "the fixer with the validator's feedback and validate again.\n"
        f"You have a strict budget of about {max_turns} model calls total; if you "
        "run out before acceptance, report validation=max_turns_exceeded.\n\n"
        "Return the final structured result (validation, report, patch_summary)."
    )


def _dry_run(
    *,
    sandbox: Sandbox,
    test_id: str,
    traceback: str,
    max_turns: int,
    reason: str,
) -> dict[str, Any]:
    """Write a VALIDATION.md describing intended behavior; no live agents."""
    file_path, _ = _split_test_id(test_id)
    note = sandbox.path / "VALIDATION.md"
    note.write_text(
        "# Sandbox fix (dry-run)\n\n"
        f"{reason}\n\n"
        f"- Failing test: `{test_id}`\n"
        f"- Test file (must NOT be modified): `{file_path}`\n"
        f"- Budget (max_turns): {max_turns}\n\n"
        "## What the fixer/validator loop would do\n\n"
        "1. **Fixer** edits only product/business code to address the failure, "
        "without touching the test and without hardcoding a pass.\n"
        "2. **Validator** diffs the sandbox and re-runs the test, enforcing:\n"
        "   - no test mutation, and\n"
        "   - no hardcoded/cheat fix.\n"
        "3. On rejection, feedback loops back to the fixer until accepted or the "
        "`max_turns` budget is exhausted.\n\n"
        "## Traceback\n\n"
        "```\n" + traceback.strip() + "\n```\n"
    )
    return {
        "sandbox_path": str(sandbox.path),
        "validation": Validation.SKIPPED.value,
        "validation_report": f"Dry-run: {reason} See {note}.",
        "patch_summary": "No changes applied (dry-run).",
    }


def _build_deep_agent(sandbox_path: Path, test_file: str, max_turns: int):
    """Construct the Deep Agents graph, or raise if deps are unavailable."""
    from deepagents import create_deep_agent  # type: ignore
    from langchain.agents.middleware import ModelCallLimitMiddleware  # type: ignore

    # LocalShellBackend gives the agents real filesystem tools AND `execute`
    # (so the validator can actually run pytest). virtual_mode=True (default)
    # scopes all paths to the sandbox root while still persisting writes to disk
    # so we can git-diff them afterwards.
    backend = None
    try:
        from deepagents.backends import LocalShellBackend  # type: ignore

        backend = LocalShellBackend(root_dir=str(sandbox_path), virtual_mode=True)
    except Exception:
        backend = None

    # Deny writes to the test tree for the fixer so it structurally cannot edit
    # the failing test (the validator still diffs as a backup). Paths are virtual
    # (rooted at the sandbox), so we include both slash and non-slash variants.
    deny_paths = [
        "tests/**",
        "/tests/**",
        "test/**",
        "/test/**",
        "**/test_*.py",
        "**/*_test.py",
        "**/conftest.py",
    ]
    if test_file:
        deny_paths.append(test_file)
        deny_paths.append("/" + test_file.lstrip("/"))
    fixer_permissions = None
    try:
        from deepagents import FilesystemPermission  # type: ignore

        fixer_permissions = [
            FilesystemPermission(operations=["write"], paths=deny_paths, mode="deny"),
        ]
    except Exception:
        fixer_permissions = None

    fixer_spec: dict[str, Any] = {
        "name": "fixer",
        "description": "Fixes product code to make the failing test pass without "
        "editing tests or hardcoding a pass.",
        "system_prompt": FIXER_SYSTEM_PROMPT,
    }
    if fixer_permissions is not None:
        fixer_spec["permissions"] = fixer_permissions

    validator_spec: dict[str, Any] = {
        "name": "validator",
        "description": "Validates the fixer's diff against the two hard rules and "
        "re-runs the failing test.",
        "system_prompt": VALIDATOR_SYSTEM_PROMPT,
    }

    model_name = default_model_name()
    kwargs: dict[str, Any] = {
        "model": model_name,
        "subagents": [fixer_spec, validator_spec],
        "middleware": [
            ModelCallLimitMiddleware(run_limit=max_turns, exit_behavior="end"),
        ],
        "response_format": SandboxResult,
        "system_prompt": (
            "You orchestrate a fixer and a validator subagent to fix a failing "
            "test safely. Delegate via the task tool; never edit tests yourself."
        ),
    }
    if backend is not None:
        kwargs["backend"] = backend

    return create_deep_agent(**kwargs)


def _extract_result(raw: Any) -> Optional[SandboxResult]:
    """Pull a SandboxResult out of a deep agent invocation response."""
    if raw is None:
        return None
    if isinstance(raw, SandboxResult):
        return raw
    if isinstance(raw, dict):
        structured = raw.get("structured_response")
        if isinstance(structured, SandboxResult):
            return structured
        if isinstance(structured, dict):
            try:
                return SandboxResult(**structured)
            except Exception:
                return None
    return None


def run_sandbox_fix(
    *,
    test_id: str,
    traceback: str,
    repo: str | Path,
    max_turns: int,
    sandbox: Optional[Sandbox] = None,
) -> dict[str, Any]:
    """Run the bounded fixer/validator loop, or a dry-run without a key."""
    sandbox = sandbox or create_sandbox(repo)
    file_path, _ = _split_test_id(test_id)

    if available_provider() is None:
        return _dry_run(
            sandbox=sandbox,
            test_id=test_id,
            traceback=traceback,
            max_turns=max_turns,
            reason="No OPENAI_API_KEY / ANTHROPIC_API_KEY set; skipping live agents.",
        )

    try:
        agent = _build_deep_agent(sandbox.path, file_path, max_turns)
    except Exception as exc:  # deepagents not installed / incompatible
        return _dry_run(
            sandbox=sandbox,
            test_id=test_id,
            traceback=traceback,
            max_turns=max_turns,
            reason=f"Deep Agents unavailable ({exc}); skipping live agents.",
        )

    prompt = _parent_prompt(test_id, file_path, traceback, max_turns)
    # Hard super-step ceiling on top of the model-call limit.
    config = {"recursion_limit": max(4, max_turns * 4)}

    try:
        raw = agent.invoke({"messages": [{"role": "user", "content": prompt}]}, config=config)
    except Exception as exc:
        # GraphRecursionError and friends land here -> treat as exceeded.
        name = type(exc).__name__
        if "Recursion" in name:
            validation = Validation.MAX_TURNS_EXCEEDED.value
            report = f"Loop hit the recursion ceiling before acceptance: {exc}"
        else:
            validation = Validation.REJECTED.value
            report = f"Sandbox loop errored: {exc}"
        return {
            "sandbox_path": str(sandbox.path),
            "validation": validation,
            "validation_report": report,
            "patch_summary": sandbox.diff()[:2000],
        }

    result = _extract_result(raw)
    diff = sandbox.diff()

    if result is None:
        # Could not parse a structured verdict; fall back to a conservative read.
        return {
            "sandbox_path": str(sandbox.path),
            "validation": Validation.MAX_TURNS_EXCEEDED.value
            if not diff.strip()
            else Validation.REJECTED.value,
            "validation_report": "No structured verdict returned by the loop.",
            "patch_summary": diff[:2000],
        }

    # Safety net: even if the loop claims acceptance, a changed test file is an
    # automatic rejection.
    validation = result.validation
    report = result.report
    if sandbox.is_git and file_path and sandbox.file_changed(file_path):
        validation = Validation.REJECTED.value
        report = (
            "Rejected by Triedge safety check: the failing test file "
            f"`{file_path}` was modified. " + report
        )

    return {
        "sandbox_path": str(sandbox.path),
        "validation": validation,
        "validation_report": report,
        "patch_summary": result.patch_summary,
        "diff": diff,
    }
