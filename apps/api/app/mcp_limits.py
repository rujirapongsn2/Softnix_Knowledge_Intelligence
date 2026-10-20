"""Token-scoped rate and concurrency limits with Redis production storage."""
import math
from collections import defaultdict
from threading import Lock
from time import monotonic, sleep, time

import redis

from .config import get_settings


class McpLimitExceeded(RuntimeError):
    def __init__(self, code: str, message: str, retry_after: float = 1.0):
        super().__init__(code)
        self.code, self.message, self.retry_after = code, message, retry_after

    @property
    def retry_after_header(self) -> dict[str, str]:
        return {"Retry-After": str(max(1, math.ceil(self.retry_after)))}


class McpLimiter:
    def __init__(self):
        self._lock = Lock()
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._active: dict[str, int] = defaultdict(int)

    def _redis(self):
        url = get_settings().redis_url
        return redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1) if url else None

    def acquire(self, token, wait_seconds: float = 0) -> None:
        """Take a slot of the token's budget, waiting up to ``wait_seconds`` for one to free up."""
        deadline = monotonic() + wait_seconds
        while True:
            try:
                return self._try_acquire(token)
            except McpLimitExceeded as exc:
                remaining = deadline - monotonic()
                if exc.code == "MCP_LIMIT_STORE_UNAVAILABLE" or remaining <= 0:
                    raise
                sleep(min(remaining, max(0.2, exc.retry_after)))

    def _try_acquire(self, token) -> None:
        key = token.id
        try:
            client = self._redis()
            if client:
                rate_key, active_key = f"skip:mcp:rate:{key}", f"skip:mcp:active:{key}"
                count = client.incr(rate_key)
                if count == 1:
                    client.expire(rate_key, 60)
                if count > token.requests_per_minute:
                    # A refused request does not use up the window it was refused in.
                    client.decr(rate_key)
                    raise McpLimitExceeded("MCP_RATE_LIMITED", "Token request rate limit exceeded", max(1, client.ttl(rate_key)))
                active = client.incr(active_key)
                client.expire(active_key, max(5, token.query_timeout_seconds + 5))
                if active > token.max_concurrent_requests:
                    client.decr(active_key)
                    raise McpLimitExceeded("MCP_CONCURRENCY_LIMITED", "Token concurrency limit exceeded")
                return
        except redis.RedisError as exc:
            # A development process without Redis still enforces limits locally.
            if get_settings().app_env == "production":
                raise McpLimitExceeded("MCP_LIMIT_STORE_UNAVAILABLE", "Rate-limit store is unavailable") from exc
        with self._lock:
            now = time()
            self._requests[key] = [item for item in self._requests[key] if item > now - 60]
            if len(self._requests[key]) >= token.requests_per_minute:
                raise McpLimitExceeded("MCP_RATE_LIMITED", "Token request rate limit exceeded", self._requests[key][0] + 60 - now)
            if self._active[key] >= token.max_concurrent_requests:
                raise McpLimitExceeded("MCP_CONCURRENCY_LIMITED", "Token concurrency limit exceeded")
            self._requests[key].append(now)
            self._active[key] += 1

    def release(self, token) -> None:
        try:
            client = self._redis()
            if client:
                client.decr(f"skip:mcp:active:{token.id}")
                return
        except redis.RedisError:
            pass
        with self._lock:
            self._active[token.id] = max(0, self._active[token.id] - 1)


mcp_limiter = McpLimiter()
