from host.server import AUTH_BLOCK_S, MAX_FAILED_AUTH_PER_WINDOW, AuthRateLimiter


def test_not_blocked_initially():
    limiter = AuthRateLimiter()
    assert limiter.is_blocked("1.2.3.4") is False


def test_blocks_after_threshold_failures():
    limiter = AuthRateLimiter()
    for _ in range(MAX_FAILED_AUTH_PER_WINDOW - 1):
        limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") is False
    limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") is True


def test_different_ips_tracked_independently():
    limiter = AuthRateLimiter()
    for _ in range(MAX_FAILED_AUTH_PER_WINDOW):
        limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") is True
    assert limiter.is_blocked("5.6.7.8") is False


def test_success_clears_failure_history():
    limiter = AuthRateLimiter()
    for _ in range(MAX_FAILED_AUTH_PER_WINDOW - 1):
        limiter.record_failure("1.2.3.4")
    limiter.record_success("1.2.3.4")
    assert limiter._failures.get("1.2.3.4") is None
    limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") is False


def test_block_expires_after_window(monkeypatch):
    import time

    limiter = AuthRateLimiter()
    for _ in range(MAX_FAILED_AUTH_PER_WINDOW):
        limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") is True

    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + AUTH_BLOCK_S + 1)
    assert limiter.is_blocked("1.2.3.4") is False
