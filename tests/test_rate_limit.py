# tests/test_rate_limit.py
"""
Tests app/auth/rate_limit.py's fixed-window logic and fail-open behavior
without a real Redis server -- see conftest.py's module docstring for
why this repo's tests load target modules in isolation rather than
mocking through a DI framework that doesn't exist here.
"""
import sys
import os
import types

import pytest
from fastapi import HTTPException, Request

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_module


class FakeRedis:
    """In-memory stand-in for exactly the three calls rate_limit.py
    makes: INCR, EXPIRE, TTL. No real expiry timing -- tests that need
    window-expiry behavior simulate it by directly manipulating
    self.store rather than sleeping."""
    def __init__(self):
        self.store = {}      # key -> count
        self.expiries = {}   # key -> seconds set via expire()

    def ping(self):
        return True

    def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key, seconds):
        self.expiries[key] = seconds

    def ttl(self, key):
        return self.expiries.get(key, -1)


def _fake_request(ip="203.0.113.5"):
    """Minimal stand-in for a Starlette Request -- rate_limit.py only
    ever reads request.client.host."""
    req = types.SimpleNamespace()
    req.client = types.SimpleNamespace(host=ip)
    return req


def test_allows_requests_under_the_limit():
    mod = load_module("app/auth/rate_limit.py")
    fake = FakeRedis()
    mod._get_redis = lambda: fake

    for _ in range(5):
        mod.check_rate_limit("test-key", max_attempts=5, window_seconds=300)
    # no exception raised for the first 5 -- if we get here, they all passed


def test_blocks_the_request_that_exceeds_the_limit():
    mod = load_module("app/auth/rate_limit.py")
    fake = FakeRedis()
    mod._get_redis = lambda: fake

    for _ in range(5):
        mod.check_rate_limit("test-key", max_attempts=5, window_seconds=300)

    with pytest.raises(HTTPException) as exc_info:
        mod.check_rate_limit("test-key", max_attempts=5, window_seconds=300)
    assert exc_info.value.status_code == 429
    assert "Retry-After" in exc_info.value.headers


def test_different_keys_have_independent_counters():
    mod = load_module("app/auth/rate_limit.py")
    fake = FakeRedis()
    mod._get_redis = lambda: fake

    for _ in range(5):
        mod.check_rate_limit("key-a", max_attempts=5, window_seconds=300)
    # key-a is now exhausted; key-b should be completely unaffected
    mod.check_rate_limit("key-b", max_attempts=5, window_seconds=300)
    with pytest.raises(HTTPException):
        mod.check_rate_limit("key-a", max_attempts=5, window_seconds=300)


def test_fails_open_when_redis_is_unavailable():
    """
    SECURITY-RELEVANT: confirms the deliberate fail-open design (see
    rate_limit.py's module docstring) -- a Redis outage must not lock
    every user out of login/password-reset.
    """
    mod = load_module("app/auth/rate_limit.py")
    mod._get_redis = lambda: None

    # Should not raise, no matter how many times it's called
    for _ in range(100):
        mod.check_rate_limit("test-key", max_attempts=1, window_seconds=300)


def test_fails_open_if_redis_call_itself_raises():
    class ExplodingRedis:
        def incr(self, key):
            raise ConnectionError("simulated Redis failure mid-request")

    mod = load_module("app/auth/rate_limit.py")
    mod._get_redis = lambda: ExplodingRedis()

    # Should not raise/propagate the ConnectionError, and should not
    # block the request either (fail open)
    mod.check_rate_limit("test-key", max_attempts=1, window_seconds=300)


def test_login_rate_limit_checks_both_ip_and_username_buckets():
    mod = load_module("app/auth/rate_limit.py")
    fake = FakeRedis()
    mod._get_redis = lambda: fake
    os.environ["LOGIN_RATE_LIMIT_PER_IP"] = "3"
    os.environ["LOGIN_RATE_LIMIT_PER_USERNAME"] = "2"
    try:
        req = _fake_request()
        mod.enforce_login_rate_limit(req, "alice")
        mod.enforce_login_rate_limit(req, "alice")
        # third call for 'alice' from the same IP should trip the
        # per-username limit (2) before the per-IP limit (3) is reached
        with pytest.raises(HTTPException) as exc_info:
            mod.enforce_login_rate_limit(req, "alice")
        assert exc_info.value.status_code == 429
    finally:
        del os.environ["LOGIN_RATE_LIMIT_PER_IP"]
        del os.environ["LOGIN_RATE_LIMIT_PER_USERNAME"]


def test_login_rate_limit_username_bucket_is_case_insensitive():
    """
    'Alice' and 'alice' must share one bucket -- otherwise an attacker
    trivially doubles their attempt budget by varying case, and a
    legitimate user who occasionally capitalizes their username
    wouldn't get any benefit from a separate bucket anyway.
    """
    mod = load_module("app/auth/rate_limit.py")
    fake = FakeRedis()
    mod._get_redis = lambda: fake
    os.environ["LOGIN_RATE_LIMIT_PER_IP"] = "100"
    os.environ["LOGIN_RATE_LIMIT_PER_USERNAME"] = "2"
    try:
        req = _fake_request()
        mod.enforce_login_rate_limit(req, "Alice")
        mod.enforce_login_rate_limit(req, "alice")
        with pytest.raises(HTTPException):
            mod.enforce_login_rate_limit(req, "ALICE")
    finally:
        del os.environ["LOGIN_RATE_LIMIT_PER_IP"]
        del os.environ["LOGIN_RATE_LIMIT_PER_USERNAME"]
