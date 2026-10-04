"""How a failed job is retried: which error codes, how many attempts, how long to wait."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    delay: Callable[[int], int]


class _Retryable(Protocol):
    attempt_count: int
    next_attempt_at: datetime


def exponential_delay(first: int, cap: int, floor: int = 0) -> Callable[[int], int]:
    """Seconds to wait after ``attempt`` failures: first, 2*first, ... up to ``cap``, never below ``floor``."""
    return lambda attempt: max(floor, min(cap, first * 2 ** max(0, attempt - 1)))


DEFAULT_RETRY = RetryPolicy(3, exponential_delay(2, 60))

# The shared provider account frees in-flight quota over minutes, so budget exhaustion earns more attempts and a longer wait.
BUDGET_RETRY = RetryPolicy(6, exponential_delay(2, 60, floor=60))

# An unreachable engine (restart, crash, deploy) is not fixed within seconds: 30s up to 5 minutes, about 27 minutes in all.
# The recovery sweep re-queues what is still failed after that.
ENGINE_DOWN_RETRY = RetryPolicy(8, exponential_delay(30, 300))

PROCESSING_RETRY_POLICIES: dict[str, RetryPolicy] = {
    **dict.fromkeys((
        "RETRIEVAL_ENGINE_REJECTED", "RETRIEVAL_ENGINE_BUSY", "RETRIEVAL_ENGINE_TIMEOUT", "OPENROUTER_UNAVAILABLE",
        "EXTERNAL_OCR_UNAVAILABLE", "EXTERNAL_OCR_TIMEOUT", "OCR_CHAIN_FAILED",
    ), DEFAULT_RETRY),
    "RETRIEVAL_ENGINE_BUDGET_EXHAUSTED": BUDGET_RETRY,
    "RETRIEVAL_ENGINE_UNAVAILABLE": ENGINE_DOWN_RETRY,
}

# A remote-index purge that gives up leaves a ghost record that blocks re-uploads of the same content.
PURGE_RETRY = RetryPolicy(8, DEFAULT_RETRY.delay)
PURGE_RETRYABLE_CODES = frozenset({"RETRIEVAL_ENGINE_BUSY", "RETRIEVAL_ENGINE_TIMEOUT", "RETRIEVAL_ENGINE_UNAVAILABLE"})


def schedule_retry(job: _Retryable, policy: RetryPolicy | None) -> bool:
    """Set the job's next attempt time and return True, or return False when it must stop for good."""
    if policy is None or job.attempt_count >= policy.max_attempts:
        return False
    job.next_attempt_at = datetime.utcnow() + timedelta(seconds=policy.delay(job.attempt_count))
    return True
