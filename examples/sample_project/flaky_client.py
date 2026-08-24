"""A simulated network client that is intermittently unavailable.

`fetch` fails part of the time with a ConnectionError, mimicking a genuinely
flaky dependency. The accompanying test therefore passes and fails across runs,
which is the canonical `retry` scenario.
"""

import random


class ConnectionError(Exception):
    """Local stand-in so the example has no external dependencies."""


class FlakyClient:
    def __init__(self, failure_rate=0.5, seed=None):
        self._failure_rate = failure_rate
        self._rng = random.Random(seed)

    def fetch(self, url):
        if self._rng.random() < self._failure_rate:
            raise ConnectionError(f"temporary failure connecting to {url}")
        return {"url": url, "status": 200, "body": "ok"}
