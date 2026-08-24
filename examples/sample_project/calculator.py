"""A tiny calculator module used by the Triedge examples.

`add` contains a deliberate bug (subtraction instead of addition) so the
accompanying test fails with a clear AssertionError. This is the canonical
"a real code fix is plausible" scenario that routes to `sandbox_fix`.
"""


def add(a, b):
    # BUG: should be `a + b`. Left here on purpose so test_add fails.
    return a - b


def subtract(a, b):
    return a - b


def multiply(a, b):
    return a * b
