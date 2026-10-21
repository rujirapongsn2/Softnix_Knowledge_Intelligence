"""The token limiter against a real Redis.

Run with TEST_REDIS_URL naming a Redis database the test may flush, for example from the api container:
    docker compose exec -e TEST_REDIS_URL=redis://redis:6379/15 api pytest tests/test_mcp_limits_redis.py"""
import os
import uuid
from types import SimpleNamespace

import pytest
import redis

from app.config import get_settings
from app.mcp_limits import McpLimitExceeded, McpLimiter

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not TEST_REDIS_URL, reason="requires TEST_REDIS_URL")


@pytest.fixture()
def store(monkeypatch):
    monkeypatch.setattr(get_settings(), "redis_url", TEST_REDIS_URL)
    client = redis.Redis.from_url(TEST_REDIS_URL)
    yield client
    client.flushdb()


def _token(rpm, concurrent):
    return SimpleNamespace(id=str(uuid.uuid4()), requests_per_minute=rpm, max_concurrent_requests=concurrent, query_timeout_seconds=60)


def _refusal(limiter, token):
    with pytest.raises(McpLimitExceeded) as refused:
        limiter.acquire(token)
    return refused.value


def test_a_concurrency_refusal_does_not_use_up_the_rate_window(store):
    limiter, token = McpLimiter(), _token(rpm=3, concurrent=1)
    limiter.acquire(token)
    for _ in range(5):
        assert _refusal(limiter, token).code == "MCP_CONCURRENCY_LIMITED"
    assert int(store.get(f"skip:mcp:rate:{token.id}")) == 1, "only the admitted request is charged"
    limiter.release(token)
    limiter.acquire(token)


def test_a_rate_refusal_reports_the_rest_of_the_window(store):
    limiter, token = McpLimiter(), _token(rpm=1, concurrent=5)
    limiter.acquire(token)
    refused = _refusal(limiter, token)
    assert refused.code == "MCP_RATE_LIMITED" and 1 <= refused.retry_after <= 60
    assert int(store.get(f"skip:mcp:rate:{token.id}")) == 1


def test_a_rate_key_without_expiry_gets_one_again(store):
    """An older limiter could leave the counter at -1 with no TTL, which refused the token forever."""
    limiter, token = McpLimiter(), _token(rpm=1, concurrent=5)
    store.set(f"skip:mcp:rate:{token.id}", 5)
    assert _refusal(limiter, token).code == "MCP_RATE_LIMITED"
    assert 0 < store.ttl(f"skip:mcp:rate:{token.id}") <= 60, "the stuck counter expires with the window"
