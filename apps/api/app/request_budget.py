"""Per-request deadline propagated to outbound model and retrieval calls."""
from contextvars import ContextVar
from time import monotonic

_deadline: ContextVar[float | None] = ContextVar("softnix_request_deadline", default=None)


def set_deadline(seconds: int):
    return _deadline.set(monotonic() + seconds)


def reset_deadline(token) -> None:
    _deadline.reset(token)


def remaining_timeout(default_seconds: float) -> float:
    deadline = _deadline.get()
    if deadline is None:
        return default_seconds
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise RuntimeError("MCP_TIMEOUT")
    return min(default_seconds, max(0.1, remaining))
