"""Routing / classification agent.

Given a failed test, its traceback summary, ephemeral history, and blame/risky
signals, choose exactly one of: sandbox_fix, quarantine, retry.

When an LLM is configured the decision is model-driven (structured output). A
deterministic rule fallback always exists so the CLI works without any API key,
and it is also used if the model call fails.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from triedge.llm import get_chat_model
from triedge.state import Decision


class RouteDecision(BaseModel):
    """Structured output emitted by the routing agent."""

    decision: Decision = Field(
        description="One of sandbox_fix, quarantine, or retry."
    )
    rationale: str = Field(
        description="Short justification citing history and signals."
    )


ROUTER_SYSTEM_PROMPT = """You are the routing agent in a failed-test triage \
system. Your ONLY job is to classify a failed unit test into exactly one of \
three actions. Do not attempt to fix anything.

Choose:
- "quarantine": the test has failed many times back-to-back (streak at or above \
the quarantine threshold). A human owner should be flagged; the test will be \
marked quarantined.
- "retry": history indicates the test is flaky (it has both passed and failed \
recently, oscillating outcomes, or the traces do not point at a stable product \
bug). Just re-run it.
- "sandbox_fix": a real code change is plausible. Strong cue: a recent or risky \
PR touched the failing code path (from git blame). Other cues: a new \
assertion/product error, no flake pattern, and a streak below the quarantine \
threshold.

Precedence: quarantine (streak) > retry (flaky) > sandbox_fix.
Respond with the structured decision and a short rationale."""


def _rule_based_decision(
    *,
    history: dict[str, Any],
    risky_signal: dict[str, Any],
    quarantine_threshold: int,
) -> RouteDecision:
    """Deterministic fallback policy."""
    consecutive = int(history.get("consecutive_failures", 0))
    looks_flaky = bool(history.get("looks_flaky", False))
    is_risky = bool(risky_signal.get("is_risky", False))

    if consecutive >= quarantine_threshold:
        return RouteDecision(
            decision=Decision.QUARANTINE,
            rationale=(
                f"{consecutive} consecutive failures >= threshold "
                f"{quarantine_threshold}; quarantining and flagging the owner."
            ),
        )

    if looks_flaky:
        return RouteDecision(
            decision=Decision.RETRY,
            rationale=(
                "History shows mixed pass/fail (flaky) with a streak below the "
                "quarantine threshold; retrying."
            ),
        )

    if is_risky:
        reasons = "; ".join(risky_signal.get("reasons", [])[:2]) or "recent change"
        return RouteDecision(
            decision=Decision.SANDBOX_FIX,
            rationale=f"Risky PR on the failing path ({reasons}); attempting a fix.",
        )

    # Default: not obviously flaky, not at quarantine threshold -> try to fix.
    return RouteDecision(
        decision=Decision.SANDBOX_FIX,
        rationale=(
            "No flake pattern and streak below quarantine threshold; a real "
            "code fix is plausible."
        ),
    )


def _build_user_prompt(
    *,
    test_id: str,
    trace_summary: str,
    history: dict[str, Any],
    blame: dict[str, Any],
    risky_signal: dict[str, Any],
    quarantine_threshold: int,
) -> str:
    top_author = ""
    for entry in blame.get("entries", []):
        if entry.get("author"):
            top_author = entry["author"]
            break
    return (
        f"Test: {test_id}\n"
        f"Quarantine threshold (consecutive failures): {quarantine_threshold}\n\n"
        f"Traceback summary:\n{trace_summary}\n\n"
        f"History:\n"
        f"- total failures: {history.get('total_failures', 0)}\n"
        f"- consecutive failures: {history.get('consecutive_failures', 0)}\n"
        f"- looks flaky: {history.get('looks_flaky', False)}\n"
        f"- recent decisions: {history.get('recent_decisions', [])}\n"
        f"- recent trace summaries: {history.get('recent_summaries', [])}\n\n"
        f"Signals:\n"
        f"- blame available: {blame.get('available', False)}\n"
        f"- top blame author: {top_author or 'n/a'}\n"
        f"- risky PR on path: {risky_signal.get('is_risky', False)}\n"
        f"- risky reasons: {risky_signal.get('reasons', [])}\n"
    )


def classify(
    *,
    test_id: str,
    trace_summary: str,
    history: dict[str, Any],
    blame: dict[str, Any],
    risky_signal: dict[str, Any],
    quarantine_threshold: int,
    model: Optional[Any] = None,
) -> RouteDecision:
    """Return a RouteDecision, LLM-driven when possible, else rule-based."""
    fallback = _rule_based_decision(
        history=history,
        risky_signal=risky_signal,
        quarantine_threshold=quarantine_threshold,
    )

    chat_model = model if model is not None else get_chat_model()
    if chat_model is None:
        return fallback

    try:
        structured = chat_model.with_structured_output(RouteDecision)
        user_prompt = _build_user_prompt(
            test_id=test_id,
            trace_summary=trace_summary,
            history=history,
            blame=blame,
            risky_signal=risky_signal,
            quarantine_threshold=quarantine_threshold,
        )
        result = structured.invoke(
            [
                {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
        )
        if isinstance(result, RouteDecision):
            return result
        # Some providers return a dict-like; coerce.
        return RouteDecision(**dict(result))
    except Exception:
        return fallback
