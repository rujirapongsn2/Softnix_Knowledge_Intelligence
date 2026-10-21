"""Token-scoped rate and concurrency limits with Redis production storage."""
import math
from collections import defaultdict
from threading import Lock
from time import time

import redis

from .config import get_settings


class McpLimitExceeded(RuntimeError):
    def __init__(self, code: str, message: str, retry_after: float = 1.0):
        super().__init__(code)
        self.code, self.message, self.retry_after = code, message, retry_after

    @property
    def retry_after_header(self) -> dict[str, str]:
        return {"Retry-After": str(max(1, math.ceil(self.retry_after)))}


# Checked and charged atomically; a refusal gives back what it took inside the script, so the give-back cannot
# land after the window's key expired (that DECR would recreate it at -1 with no TTL and refuse the token forever).
# Only an admitted request renews the active key's expiry: a count leaked by a killed request must run out even
# while its client keeps retrying.
_ACQUIRE = """
local rate = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then redis.call('EXPIRE', KEYS[1], 60) end
if rate > tonumber(ARGV[1]) then
  redis.call('DECR', KEYS[1])
  return {1, redis.call('TTL', KEYS[1])}
end
local active = redis.call('INCR', KEYS[2])
if active > tonumber(ARGV[2]) then
  redis.call('DECR', KEYS[2])
  redis.call('DECR', KEYS[1])
  if redis.call('TTL', KEYS[2]) < 0 then redis.call('EXPIRE', KEYS[2], ARGV[3]) end
  return {2, 1}
end
redis.call('EXPIRE', KEYS[2], ARGV[3])
return {0, 0}
"""

# A request that outlived the active key's expiry must not drive the count below zero.
_RELEASE = """
local active = tonumber(redis.call('GET', KEYS[1]) or '0')
if active > 0 then return redis.call('DECR', KEYS[1]) end
return 0
"""


class McpLimiter:
    def __init__(self):
        self._lock = Lock()
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._active: dict[str, int] = defaultdict(int)
        self._scripts: dict[str, tuple] = {}  # redis URL -> (acquire, release)

    def _redis(self):
        """The Redis client and its two scripts for the configured URL, built once and reused."""
        url = get_settings().redis_url
        if not url:
            return None
        if url not in self._scripts:
            client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
            self._scripts[url] = (client.register_script(_ACQUIRE), client.register_script(_RELEASE))
        return self._scripts[url]

    def acquire(self, token) -> None:
        key = token.id
        try:
            scripts = self._redis()
            if scripts:
                refused, retry_after = scripts[0](keys=[f"skip:mcp:rate:{key}", f"skip:mcp:active:{key}"],
                                                  args=[token.requests_per_minute, token.max_concurrent_requests,
                                                        max(5, token.query_timeout_seconds + 5)])
                if refused == 1:
                    raise McpLimitExceeded("MCP_RATE_LIMITED", "Token request rate limit exceeded", max(1, retry_after))
                if refused == 2:
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
            scripts = self._redis()
            if scripts:
                scripts[1](keys=[f"skip:mcp:active:{token.id}"])
                return
        except redis.RedisError:
            pass
        with self._lock:
            self._active[token.id] = max(0, self._active[token.id] - 1)


mcp_limiter = McpLimiter()
