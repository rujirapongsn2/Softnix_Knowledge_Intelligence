"""Pause the jobs that need a provider while its account cannot serve them, and resume them on their own.

Retrying does not fix a credit or key failure. Without a hold every queued document would take a worker,
re-extract and re-embed its text, fail the same way and end up failed, waiting for a person to press
Process again on each one. Instead the first such failure places a hold on the provider: claim_next_job
skips the job types that need it, the failed job goes back to the queue without spending an attempt, and a
probe that backs off from one minute to ten lifts the hold as soon as the provider answers (credits topped
up, key replaced).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .embeddings import EmbeddingClient
from .llm import LLMClient
from .models import JobType, ProcessingJob, ProviderHold

logger = logging.getLogger(__name__)

# LightRAG reports the LLM provider's 402 (no credits, or the in-flight budget) as RETRIEVAL_ENGINE_BUDGET_EXHAUSTED.
HOLD_CODES = {
    "LLM_CREDITS_EXHAUSTED": "llm", "LLM_AUTH_FAILED": "llm", "RETRIEVAL_ENGINE_BUDGET_EXHAUSTED": "llm",
    "EMBEDDING_CREDITS_EXHAUSTED": "embedding", "EMBEDDING_AUTH_FAILED": "embedding",
}
_PIPELINE = frozenset({"llm", "embedding"})
JOB_PROVIDERS: dict[str, frozenset[str]] = {
    JobType.PROCESS_DOCUMENT: _PIPELINE, JobType.REPROCESS_DOCUMENT: _PIPELINE,
    JobType.RESTORE_DOCUMENT: _PIPELINE, JobType.AUTO_RETRY_DOCUMENT: _PIPELINE,
    JobType.REINDEX_EMBEDDINGS: frozenset({"embedding"}),
    JobType.EXTRACT_DOCUMENT_METADATA: frozenset({"llm"}), JobType.EXTRACT_LEGAL_METADATA: frozenset({"llm"}),
    JobType.REBUILD_LEGAL_GRAPH: frozenset({"llm"}), JobType.PURGE_REMOTE_INDEX: frozenset(),
}
FIRST_PROBE_SECONDS = 60
MAX_PROBE_SECONDS = 600


def _probe_llm() -> str | None:
    return LLMClient().probe()


def _probe_embedding() -> str | None:
    report = EmbeddingClient().check()
    return None if report.get("embedding_model_available") else report.get("embedding_error", "EMBEDDING_UNAVAILABLE")


# Each probe returns None when the provider serves requests again, else the code it failed with.
PROBES: dict[str, Callable[[], str | None]] = {"llm": _probe_llm, "embedding": _probe_embedding}


def provider_of(code: str | None) -> str | None:
    return HOLD_CODES.get(code or "")


def place_hold(db: Session, provider: str, code: str) -> None:
    """Record the hold in its own savepoint; a hold another worker placed first is kept as it is."""
    if db.get(ProviderHold, provider) is not None:
        return
    now = datetime.utcnow()
    try:
        with db.begin_nested():
            db.add(ProviderHold(provider=provider, reason_code=code, held_since=now,
                                next_probe_at=now + timedelta(seconds=FIRST_PROBE_SECONDS)))
        logger.warning("provider hold placed", extra={"provider": provider, "error_code": code})
    except IntegrityError:
        pass


def wait_for_provider(db: Session, job: ProcessingJob, code: str) -> None:
    """Hold the job's provider and put the job back in the queue without spending the attempt it just made."""
    place_hold(db, provider_of(code), code)
    job.status, job.current_stage = "queued", "waiting_for_provider"
    job.attempt_count = max(0, job.attempt_count - 1)
    job.next_attempt_at = datetime.utcnow()
    job.error_code = code
    job.error_message = "Waiting for the AI provider (credits or API key); the job continues on its own once it answers again."


def held_job_types(db: Session) -> list[str]:
    held = {hold.provider for hold in db.query(ProviderHold).all()}
    return [job_type for job_type, providers in JOB_PROVIDERS.items() if providers & held] if held else []


def probe_holds(db: Session, now: datetime | None = None) -> list[str]:
    """Probe every hold that is due; lift the ones whose provider answers and return their providers."""
    now = now or datetime.utcnow()
    lifted = []
    for hold in db.query(ProviderHold).filter(ProviderHold.next_probe_at <= now).all():
        error = PROBES[hold.provider]()
        if error is None:
            db.delete(hold)
            lifted.append(hold.provider)
            logger.warning("provider hold lifted", extra={"provider": hold.provider, "held_since": hold.held_since.isoformat()})
        else:
            hold.probe_count += 1
            hold.last_probe_error = error
            hold.next_probe_at = now + timedelta(seconds=min(MAX_PROBE_SECONDS, FIRST_PROBE_SECONDS * 2 ** hold.probe_count))
    db.commit()
    return lifted


def hold_view(db: Session) -> list[dict]:
    return [{"provider": hold.provider, "reason_code": hold.reason_code, "held_since": hold.held_since,
             "next_probe_at": hold.next_probe_at, "probe_count": hold.probe_count, "last_probe_error": hold.last_probe_error}
            for hold in db.query(ProviderHold).order_by(ProviderHold.held_since).all()]
