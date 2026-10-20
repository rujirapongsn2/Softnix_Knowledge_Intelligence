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

# Provider outages (the LLM or the embedding server) and rate limits (429) are not fixed within seconds either:
# wait at least a minute, doubling to 15 minutes, six attempts (about half an hour in all).  The job is
# re-queued with a later next_attempt_at, so the worker keeps serving other jobs meanwhile.
PROVIDER_RETRY = RetryPolicy(6, exponential_delay(60, 900, floor=60))

# Credit, key and in-flight budget failures (402, 401) are not retried here: provider_holds.py pauses every job
# that needs the provider and resumes them once it answers again.

# An unreachable engine (restart, crash, deploy) is not fixed within seconds: 30s up to 5 minutes, about 27 minutes in all.
# The recovery sweep re-queues what is still failed after that.
ENGINE_DOWN_RETRY = RetryPolicy(8, exponential_delay(30, 300))

PROCESSING_RETRY_POLICIES: dict[str, RetryPolicy] = {
    **dict.fromkeys((
        "RETRIEVAL_ENGINE_REJECTED", "RETRIEVAL_ENGINE_BUSY", "RETRIEVAL_ENGINE_TIMEOUT",
        "EXTERNAL_OCR_UNAVAILABLE", "EXTERNAL_OCR_TIMEOUT", "OCR_CHAIN_FAILED",
    ), DEFAULT_RETRY),
    **dict.fromkeys(("LLM_UNAVAILABLE", "EMBEDDING_UNAVAILABLE"), PROVIDER_RETRY),
    "RETRIEVAL_ENGINE_UNAVAILABLE": ENGINE_DOWN_RETRY,
}

# A purge postponed because LightRAG is busy indexing: 15s doubling to 5 minutes, for up to about 16 hours
# (indexing can take up to 900s per document across several workers).  Busy answers are counted in
# deferral_count, not as failed attempts.
PURGE_MAX_BUSY_DEFERRALS = 200


def purge_busy_delay(deferrals: int) -> int:
    return min(300, 15 * 2 ** max(0, min(deferrals, 6) - 1))


# A remote-index purge that gives up leaves a ghost record that blocks re-uploads of the same content.
PURGE_RETRY = RetryPolicy(8, DEFAULT_RETRY.delay)
PURGE_RETRYABLE_CODES = frozenset({"RETRIEVAL_ENGINE_BUSY", "RETRIEVAL_ENGINE_TIMEOUT", "RETRIEVAL_ENGINE_UNAVAILABLE"})


def schedule_retry(job: _Retryable, policy: RetryPolicy | None) -> bool:
    """Set the job's next attempt time and return True, or return False when it must stop for good."""
    if policy is None or job.attempt_count >= policy.max_attempts:
        return False
    job.next_attempt_at = datetime.utcnow() + timedelta(seconds=policy.delay(job.attempt_count))
    return True
