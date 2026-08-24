from flaky_client import FlakyClient


def test_fetch_returns_ok():
    # No fixed seed on purpose: this test is genuinely flaky and will pass on
    # some runs and fail on others, producing the mixed pass/fail history that
    # routes to `retry`.
    client = FlakyClient(failure_rate=0.5)
    result = client.fetch("https://example.com/api")
    assert result["status"] == 200
