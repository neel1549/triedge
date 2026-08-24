"""Triedge CLI: triage a failed unit test into sandbox_fix, quarantine, or retry.

Examples:
    python main.py route --test tests/foo.py::test_bar --traceback-file tb.txt
    python main.py route --test tests/foo.py::test_bar --traceback "AssertionError: ..."
    python main.py show-history --test tests/foo.py::test_bar --history-file hist.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

from triedge.graph import build_graph
from triedge.llm import available_provider, get_chat_model
from triedge.state import DEFAULT_MAX_TURNS, DEFAULT_QUARANTINE_THRESHOLD
from triedge.store import InMemoryStore


def _read_traceback(args: argparse.Namespace) -> str:
    if args.traceback_file:
        path = Path(args.traceback_file)
        if not path.exists():
            print(f"error: traceback file not found: {path}", file=sys.stderr)
            sys.exit(2)
        return path.read_text()
    if args.traceback:
        return args.traceback
    # Allow piping a traceback on stdin.
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data.strip():
            return data
    return ""


def _max_turns(args: argparse.Namespace) -> int:
    if args.max_turns is not None:
        return max(1, args.max_turns)
    env = os.environ.get("TRIEDGE_MAX_TURNS")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return DEFAULT_MAX_TURNS


def cmd_route(args: argparse.Namespace) -> int:
    traceback = _read_traceback(args)
    if not traceback.strip():
        print(
            "warning: no traceback provided (use --traceback/--traceback-file/stdin)",
            file=sys.stderr,
        )

    store = InMemoryStore(args.history_file)
    model = get_chat_model()  # None without an API key
    graph = build_graph(store, model=model)

    initial = {
        "test_id": args.test,
        "traceback": traceback,
        "repo": str(Path(args.repo).resolve()),
        "max_turns": _max_turns(args),
        "quarantine_threshold": args.quarantine_threshold,
    }
    config = {"configurable": {"thread_id": f"triedge-{uuid.uuid4().hex[:8]}"}}
    final = graph.invoke(initial, config=config)

    _print_route_result(final, json_output=args.json)
    return 0


def _print_route_result(final: dict, *, json_output: bool) -> None:
    summary = {
        "test_id": final.get("test_id"),
        "decision": final.get("decision"),
        "rationale": final.get("rationale"),
        "trace_summary": final.get("trace_summary"),
        "risky_signal": final.get("risky_signal", {}),
        "sandbox_path": final.get("sandbox_path"),
        "patch_summary": final.get("patch_summary"),
        "slack_path": final.get("slack_path"),
        "validation": final.get("validation"),
        "validation_report": final.get("validation_report"),
        "retry_result": final.get("retry_result"),
    }
    if json_output:
        print(json.dumps(summary, indent=2, default=str))
        return

    provider = available_provider() or "none (deterministic fallback)"
    print("=" * 68)
    print(f"Triedge decision for: {summary['test_id']}")
    print("=" * 68)
    print(f"LLM provider     : {provider}")
    print(f"Decision         : {summary['decision']}")
    print(f"Rationale        : {summary['rationale']}")
    print(f"Trace summary    : {summary['trace_summary']}")
    risky = summary["risky_signal"] or {}
    print(f"Risky PR signal  : {risky.get('is_risky', False)}")
    for reason in risky.get("reasons", [])[:3]:
        print(f"    - {reason}")

    decision = summary["decision"]
    if decision == "sandbox_fix":
        print("-" * 68)
        print(f"Sandbox path     : {summary['sandbox_path']}")
        print(f"Validation       : {summary['validation']}")
        print(f"Patch summary    : {summary['patch_summary']}")
        print(f"Validation report: {summary['validation_report']}")
    elif decision == "quarantine":
        print("-" * 68)
        print(f"Sandbox path     : {summary['sandbox_path']}")
        print(f"Patch summary    : {summary['patch_summary']}")
        print(f"Slack alert      : {summary['slack_path']}")
    elif decision == "retry":
        print("-" * 68)
        retry = summary["retry_result"] or {}
        print(f"Retry outcome    : {retry.get('outcome')}")
        if retry.get("details"):
            print("Retry details    :")
            for line in str(retry["details"]).splitlines():
                print(f"    {line}")
    print("=" * 68)


def cmd_show_history(args: argparse.Namespace) -> int:
    store = InMemoryStore(args.history_file)
    history = store.get(args.test)
    payload = history.summary()
    payload["records"] = [r.to_dict() for r in history.records]
    print(json.dumps(payload, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triedge",
        description="Triage failed unit tests: sandbox_fix, quarantine, or retry.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_route = sub.add_parser("route", help="Classify and act on a failed test.")
    p_route.add_argument("--test", required=True, help="Test id: path::nodeid")
    p_route.add_argument("--traceback", help="Inline traceback text.")
    p_route.add_argument("--traceback-file", help="Path to a file with the traceback.")
    p_route.add_argument("--history-file", help="JSON file to seed/persist history.")
    p_route.add_argument("--repo", default=".", help="Repo root (default: cwd).")
    p_route.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help=f"Sandbox loop cap (default {DEFAULT_MAX_TURNS} or TRIEDGE_MAX_TURNS).",
    )
    p_route.add_argument(
        "--quarantine-threshold",
        type=int,
        default=DEFAULT_QUARANTINE_THRESHOLD,
        help="Consecutive failures that trigger quarantine (clamped 10-20).",
    )
    p_route.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_route.set_defaults(func=cmd_route)

    p_hist = sub.add_parser("show-history", help="Show stored history for a test.")
    p_hist.add_argument("--test", required=True, help="Test id: path::nodeid")
    p_hist.add_argument("--history-file", help="JSON history file to read.")
    p_hist.set_defaults(func=cmd_show_history)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
