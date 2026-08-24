# Triedge: failed-test triage workflow

Triedge is a local, multi-agent workflow that decides what to do about a failed
unit test. A LangGraph routing agent loads prior-failure memory for the test and
routes to exactly one of three paths:

1. **sandbox_fix** — spin up a bounded Deep Agents fixer/validator loop that
   proposes a real product-code fix in an isolated sandbox.
2. **quarantine** — the test has failed many times back-to-back; add a
   quarantine decorator in the sandbox and flag the test writer in Slack.
3. **retry** — history indicates the test is just flaky; re-run it.

This is a v1, local-only implementation. GitHub Actions, live Slack, and Docker
isolation are out of scope; those integrations plug into the same three graph
edges later.

## Inputs

The routing agent classifies using:

- **test identity** — `path::nodeid` (e.g. `tests/foo.py::test_bar`).
- **stack trace** — the raw traceback of the failure.
- **ephemeral history** — from an in-memory store keyed by test id: prior
  failures, the consecutive-fail streak, summarized traces, and past
  decisions/outcomes.
- **signals** — `git blame` on the frames in the traceback, plus a lightweight
  "risky PR" heuristic (recent commits on the failing code path, optional
  `.triedge/risky_prs.json` fixture).

## Decision policy (router)

The routing agent's only job is to classify. It emits a structured
`RouteDecision`. When an LLM is available (`OPENAI_API_KEY` or
`ANTHROPIC_API_KEY`) the decision is model-driven with the rules below as
guidance; otherwise a deterministic rule-based fallback runs so the CLI works
without any API key.

- **quarantine** — the consecutive-failure streak in history hits a configurable
  threshold (default **10**, clamped to the 10–20 range). Downstream: sandboxed
  edit that adds a quarantine decorator, then a Slack flag to the test writer
  (identified via blame).
- **retry** — history looks flaky: mixed pass/fail after retries, oscillating
  outcomes, or summarized traces that do not point at a stable product bug.
- **sandbox_fix** — a real code change is plausible. Primary cue: blame on the
  failing path hits a recent / risky PR. Other cues: a new assertion/product
  error, no flake pattern, and a streak below the quarantine threshold.

Precedence in the rule fallback: quarantine (streak) > retry (flaky) >
sandbox_fix (default when a fix looks plausible).

## Sandbox multi-agent loop (fixer + validator)

When the router picks `sandbox_fix`, Triedge runs a Deep Agents graph
(`create_deep_agent` from `deepagents`) rooted at the sandbox worktree via a
filesystem backend. Two subagents, orchestrated by a thin parent:

- **fixer** — edits product/business code to address the failure. Its system
  prompt forbids:
  - changing the failing test (or sibling tests / `conftest` helpers), and
  - hardcoding or stubbing business logic just to get a green run — e.g. magic
    constants, `return True`, swallowing exceptions, or copying the assertion's
    expected value into production code.
- **validator** — reviews the sandbox diff and re-runs the failing test. It must
  explicitly check both hard rules:
  1. **No test mutation** — the failing test file (and test helpers) are
     unchanged versus the worktree baseline.
  2. **No hardcoded pass** — production changes look like a real fix, not a cheat
     to satisfy the assertion.

  It reports `accepted` or `rejected` with concrete file/line reasons. On
  reject, the feedback goes back to the fixer for another attempt.

Structural backup for rule 1: the fixer's filesystem permissions deny
write/edit on the failing test path (and `tests/**` where possible). The
validator still diffs, because permissions can miss helpers.

### Loop cap: `max_turns`

`create_deep_agent` has no constructor argument named `max_turns`. The official
Deep Agents loop limiter is LangChain middleware, which Triedge exposes as its
`max_turns` config:

```python
from deepagents import create_deep_agent
from langchain.agents.middleware import ModelCallLimitMiddleware

agent = create_deep_agent(
    model=...,
    backend=FilesystemBackend(root_dir=sandbox_path),
    subagents=[fixer_spec, validator_spec],
    middleware=[ModelCallLimitMiddleware(run_limit=max_turns, exit_behavior="end")],
)
```

- CLI `--max-turns` / `TRIEDGE_MAX_TURNS`, default **8**.
- `exit_behavior="end"` so hitting the cap is a graceful `max_turns_exceeded`
  outcome, not a crash.
- The invoke config also sets `recursion_limit = max_turns * 4` as a hard
  LangGraph super-step ceiling (parent + subagent steps). A `GraphRecursionError`
  is caught and persisted as the same exceeded outcome.

Without an API key, Triedge skips the live loop and writes a dry-run
`VALIDATION.md` under the sandbox describing what the fixer/validator would do.

## v1 surface (local only)

- **CLI:**
  - `python main.py route --test tests/foo.py::test_bar --traceback-file tb.txt`
    - `--traceback` / `--traceback-file` — the failure traceback (inline or file).
    - `--history-file` — optional JSON to seed/persist the store across runs.
      Default store is in-memory for the run.
    - `--max-turns` (default 8, or `TRIEDGE_MAX_TURNS`) — caps the sandbox loop.
    - `--repo` — repository root for git blame / sandboxing (default: cwd).
  - `python main.py show-history --test tests/foo.py::test_bar [--history-file f]`
- **Git blame:** real `git blame` / `git log` when the file exists in a git repo;
  otherwise a stub that returns an empty blame result.
- **Risky PRs:** heuristic from blame (commit age) plus an optional
  `.triedge/risky_prs.json` fixture listing risky commit shas / PR numbers.
- **Slack:** writes a markdown message to `.triedge/slack/` (channel, author,
  test, reason). No Slack API call.
- **Sandbox:** a temp git worktree, or a `tempfile` copy when the repo is not a
  git repo. All edits stay inside the sandbox; nothing is committed.
- **Retry:** invokes `pytest <nodeid>` when pytest is available; records
  pass/fail. If pytest is missing, records a dry-run retry result.

## LangGraph shape

Typed state (`triedge/state.py`):

- `test_id`, `traceback`, `trace_summary`
- `history` (streak, recent records, prior summaries)
- `blame`, `risky_signal`
- `decision` (`sandbox_fix` | `quarantine` | `retry`) + `rationale`
- `sandbox_path`, `patch_summary`, `slack_path`, `retry_result`
- `validation` (`accepted` | `rejected` | `max_turns_exceeded`) + `validation_report`
- `max_turns`

Graph (`triedge/graph.py`):

1. `load_context` — fetch store records, summarize the traceback (first error
   line + top frames).
2. `gather_signals` — blame + risky-PR tools (not an LLM).
3. `route` — routing node with structured output (`RouteDecision`); LLM when a
   key is present, else the rule fallback.
4. Conditional edge to one worker (`sandbox_fix` | `quarantine` | `retry`).
5. `persist` — append this failure + decision + outcome to the store (and JSON
   when `--history-file` is set).

Checkpointer: LangGraph `MemorySaver` for the run. Cross-run test memory is the
custom failure store, not graph checkpoints. Router, quarantine, and retry are
plain Python nodes; only `sandbox_fix` is a nested Deep Agents graph.

## Package layout

- `spec.md` — this document.
- `main.py` — argparse CLI that builds the graph and prints the decision.
- `triedge/state.py` — typed state and enums.
- `triedge/store.py` — in-memory map `test_id -> TestHistory`; optional JSON
  load/save.
- `triedge/router.py` — prompt + structured schema + rule fallback.
- `triedge/graph.py` — the LangGraph assembly.
- `triedge/agents/sandbox.py` — Deep Agents parent + fixer/validator subagents,
  `max_turns` wiring.
- `triedge/agents/quarantine.py` — add `@pytest.mark.quarantine` above the
  failing test; identify the writer via blame.
- `triedge/agents/retry.py` — re-run the single test, capture the outcome.
- `triedge/tools/git_blame.py`, `triedge/tools/slack.py`.
- `requirements.txt` — `langgraph`, `langchain-core`, `deepagents`, `pydantic`;
  optional `langchain-openai` / `langchain-anthropic`.

## Quarantine and Slack details

- Decorator: `@pytest.mark.quarantine` plus a comment with the reason and streak
  count. If the test file is not found, a `QUARANTINE_PATCH.md` is written in the
  sandbox instead of failing the graph.
- Slack stub body: test id, writer (blame author), consecutive fail count, and a
  link/path to the sandbox diff.

## Out of scope for this pass

Live Slack, CI webhooks, Docker isolation, auto-merge of accepted fixes, and
multi-test batching.
