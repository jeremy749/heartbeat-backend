"""The sliding-window failure limiter, on its own."""

from __future__ import annotations

from app.ratelimit import MAX_TRACKED_KEYS, FailureLimiter


def test_allows_up_to_the_limit():
    lim = FailureLimiter(max_failures=3, window_seconds=60)
    for _ in range(3):
        assert lim.retry_after("k") == 0.0
        lim.record_failure("k")
    assert lim.retry_after("k") > 0


def test_retry_after_never_exceeds_the_window():
    lim = FailureLimiter(max_failures=1, window_seconds=30)
    lim.record_failure("k")
    assert 0 < lim.retry_after("k") <= 30


def test_keys_are_independent():
    lim = FailureLimiter(max_failures=1, window_seconds=60)
    lim.record_failure("a")
    assert lim.retry_after("a") > 0
    assert lim.retry_after("b") == 0.0


def test_reset_clears_one_key_only():
    lim = FailureLimiter(max_failures=1, window_seconds=60)
    lim.record_failure("a")
    lim.record_failure("b")
    lim.reset("a")
    assert lim.retry_after("a") == 0.0
    assert lim.retry_after("b") > 0


def test_failures_age_out_of_the_window(monkeypatch):
    """Drive the clock rather than sleeping, so the test stays fast."""
    now = [1000.0]
    monkeypatch.setattr("app.ratelimit.time.monotonic", lambda: now[0])

    lim = FailureLimiter(max_failures=2, window_seconds=60)
    lim.record_failure("k")
    lim.record_failure("k")
    assert lim.retry_after("k") > 0

    now[0] += 61
    assert lim.retry_after("k") == 0.0


def test_window_slides_rather_than_resetting(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("app.ratelimit.time.monotonic", lambda: now[0])

    lim = FailureLimiter(max_failures=2, window_seconds=60)
    lim.record_failure("k")
    now[0] += 40
    lim.record_failure("k")
    assert lim.retry_after("k") > 0

    now[0] += 21  # the first failure ages out, the second has not
    assert lim.retry_after("k") == 0.0


def test_tracked_keys_do_not_grow_without_bound(monkeypatch):
    """A client rotating keys must not be able to pin memory."""
    now = [1000.0]
    monkeypatch.setattr("app.ratelimit.time.monotonic", lambda: now[0])

    lim = FailureLimiter(max_failures=1, window_seconds=10)
    for i in range(MAX_TRACKED_KEYS + 100):
        lim.record_failure(f"key-{i}")
        now[0] += 1  # each one ages out well before the sweep

    assert len(lim._hits) <= MAX_TRACKED_KEYS


def test_clear_forgets_everything():
    lim = FailureLimiter(max_failures=1, window_seconds=60)
    lim.record_failure("a")
    lim.clear()
    assert lim.retry_after("a") == 0.0
