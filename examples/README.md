# Triedge examples

Example failed-test traces and a small buggy sample project so the failing-test
behavior Triedge triages is visible, reproducible, and debuggable.

## Layout

```
examples/
  sample_project/            A tiny project whose tests genuinely fail
    calculator.py            add() has a bug (subtraction) -> AssertionError
    inventory.py             reserve() has a bug -> KeyError on unknown SKU
    flaky_client.py          fetch() fails ~50% of the time (genuinely flaky)
    tests/                   the failing/flaky tests
    requirements.txt         pytest
  traces/                    curated, portable tracebacks fed to Triedge
    calculator_assertion.txt   product bug -> routes to sandbox_fix
    inventory_keyerror.txt     product bug -> routes to sandbox_fix
    flaky_network_error.txt    flaky dependency -> routes to retry
    import_error.txt           missing module at collection time
    pytest_output/             raw `pytest` output for the same failures
  history/                   seed histories that drive quarantine / retry routes
    quarantine_seed.json       12 consecutive failures -> quarantine
    flaky_seed.json            mixed pass/fail -> retry
```

The files in `traces/` are native-style Python tracebacks with repo-relative
paths (the form you would capture from an app or CI). Triedge's blame parser
reads the frames from these to run `git blame` on the failing code path. The
`traces/pytest_output/` files are the raw `pytest` short-form outputs for the
same failures, kept for readability.

## Reproduce the failures yourself

```bash
cd examples/sample_project
python -m pip install -r requirements.txt
PYTHONPATH=. python -m pytest -q            # calculator + inventory fail; flaky varies
```

- `tests/test_calculator.py::test_add` fails deterministically (the `add` bug).
- `tests/test_inventory.py::test_reserve_unknown_sku_returns_false` fails
  deterministically (the `reserve` bug).
- `tests/test_flaky_network.py::test_fetch_returns_ok` passes/fails at random.

## Run Triedge on the examples

From the repo root, with the virtualenv active (see the top-level README):

### 1. sandbox_fix (a real code fix is plausible)

```bash
python main.py route \
  --test "tests/test_calculator.py::test_add" \
  --traceback-file examples/traces/calculator_assertion.txt \
  --repo examples/sample_project
```

With no API key this prints the decision and writes a dry-run `VALIDATION.md`
into a sandbox. With `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` set, the bounded
fixer/validator loop edits `calculator.py` (never the test) in the sandbox.

### 2. quarantine (failing repeatedly back-to-back)

```bash
python main.py route \
  --test "tests/test_calculator.py::test_add" \
  --traceback-file examples/traces/calculator_assertion.txt \
  --repo examples/sample_project \
  --history-file examples/history/quarantine_seed.json
```

Adds `@pytest.mark.quarantine` above the test in a sandbox and writes a Slack
(stub) alert under `examples/sample_project/.triedge/slack/`.

### 3. retry (flaky)

```bash
python main.py route \
  --test "tests/test_flaky_network.py::test_fetch_returns_ok" \
  --traceback-file examples/traces/flaky_network_error.txt \
  --repo examples/sample_project \
  --history-file examples/history/flaky_seed.json
```

Re-runs the test with pytest when available; otherwise records a dry-run retry.

## Notes

- The seed history files are appended to on each run (the store persists the new
  failure). Copy them first if you want to keep the originals pristine.
- Blame/"risky PR" signals require the sample project's files to be committed to
  git; they are, as part of this repository.
