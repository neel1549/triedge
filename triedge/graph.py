"""LangGraph assembly for the Triedge triage workflow.

Flow: load_context -> gather_signals -> route -> {sandbox_fix | quarantine |
retry} -> persist.

Only `route` and the `sandbox_fix` worker are LLM-driven (and both degrade to
deterministic behavior without an API key). `gather_signals`, `quarantine`, and
`retry` are plain Python.
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from triedge.agents.quarantine import run_quarantine
from triedge.agents.retry import run_retry
from triedge.agents.sandbox import run_sandbox_fix
from triedge.router import classify
from triedge.state import (
    Decision,
    RetryOutcome,
    TriageState,
    Validation,
    clamp_quarantine_threshold,
)
from triedge.store import InMemoryStore
from triedge.summarize import summarize_traceback
from triedge.tools.git_blame import assess_risk, blame_traceback


def build_graph(store: InMemoryStore, model: Optional[Any] = None):
    """Compile the triage graph, capturing the store and optional LLM."""

    def load_context(state: TriageState) -> dict[str, Any]:
        test_id = state["test_id"]
        traceback = state.get("traceback", "")
        threshold = clamp_quarantine_threshold(
            state.get("quarantine_threshold") or 10
        )
        return {
            "trace_summary": summarize_traceback(traceback),
            "history": store.summary(test_id),
            "quarantine_threshold": threshold,
        }

    def gather_signals(state: TriageState) -> dict[str, Any]:
        repo = state.get("repo", ".")
        traceback = state.get("traceback", "")
        blame = blame_traceback(traceback, repo)
        risky = assess_risk(blame, repo)
        return {"blame": blame.to_dict(), "risky_signal": risky.to_dict()}

    def route(state: TriageState) -> dict[str, Any]:
        decision = classify(
            test_id=state["test_id"],
            trace_summary=state.get("trace_summary", ""),
            history=state.get("history", {}),
            blame=state.get("blame", {}),
            risky_signal=state.get("risky_signal", {}),
            quarantine_threshold=state.get("quarantine_threshold", 10),
            model=model,
        )
        return {
            "decision": decision.decision.value,
            "rationale": decision.rationale,
        }

    def sandbox_fix_node(state: TriageState) -> dict[str, Any]:
        result = run_sandbox_fix(
            test_id=state["test_id"],
            traceback=state.get("traceback", ""),
            repo=state.get("repo", "."),
            max_turns=state.get("max_turns", 8),
        )
        return {
            "sandbox_path": result.get("sandbox_path", ""),
            "validation": result.get("validation", ""),
            "validation_report": result.get("validation_report", ""),
            "patch_summary": result.get("patch_summary", ""),
        }

    def quarantine_node(state: TriageState) -> dict[str, Any]:
        history = state.get("history", {})
        result = run_quarantine(
            test_id=state["test_id"],
            repo=state.get("repo", "."),
            consecutive_failures=int(history.get("consecutive_failures", 0)),
            reason=state.get("rationale", ""),
            blame=state.get("blame", {}),
        )
        return {
            "sandbox_path": result.get("sandbox_path", ""),
            "patch_summary": result.get("patch_summary", ""),
            "slack_path": result.get("slack_path", ""),
        }

    def retry_node(state: TriageState) -> dict[str, Any]:
        result = run_retry(state["test_id"], state.get("repo", "."))
        return {"retry_result": result}

    def persist(state: TriageState) -> dict[str, Any]:
        decision = state.get("decision", "")
        outcome = ""
        passed: Optional[bool] = None

        if decision == Decision.RETRY.value:
            retry_result = state.get("retry_result", {})
            outcome = retry_result.get("outcome", "")
            passed = retry_result.get("passed")
        elif decision == Decision.SANDBOX_FIX.value:
            outcome = state.get("validation", "")
            if outcome == Validation.ACCEPTED.value:
                passed = True
        elif decision == Decision.QUARANTINE.value:
            outcome = "quarantined"

        store.record_failure(
            test_id=state["test_id"],
            trace_summary=state.get("trace_summary", ""),
            decision=decision,
            outcome=outcome,
            passed=passed,
        )
        store.save()
        return {}

    def route_edge(state: TriageState) -> str:
        return state.get("decision", Decision.RETRY.value)

    graph = StateGraph(TriageState)
    graph.add_node("load_context", load_context)
    graph.add_node("gather_signals", gather_signals)
    graph.add_node("route", route)
    graph.add_node("sandbox_fix", sandbox_fix_node)
    graph.add_node("quarantine", quarantine_node)
    graph.add_node("retry", retry_node)
    graph.add_node("persist", persist)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "gather_signals")
    graph.add_edge("gather_signals", "route")
    graph.add_conditional_edges(
        "route",
        route_edge,
        {
            Decision.SANDBOX_FIX.value: "sandbox_fix",
            Decision.QUARANTINE.value: "quarantine",
            Decision.RETRY.value: "retry",
        },
    )
    graph.add_edge("sandbox_fix", "persist")
    graph.add_edge("quarantine", "persist")
    graph.add_edge("retry", "persist")
    graph.add_edge("persist", END)

    return graph.compile(checkpointer=MemorySaver())
