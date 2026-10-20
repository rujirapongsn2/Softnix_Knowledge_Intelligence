"""Claiming, ownership, heartbeats and stale-job recovery for the processing queue.

Several worker processes share the ``processing_jobs`` table.

* A job is claimed atomically (``FOR UPDATE SKIP LOCKED`` on PostgreSQL) and
  gets a fresh ``claim_token``.  While the job runs, every commit of the
  worker's session first checks that the row still carries that token
  (``own_job``), so a worker whose job was swept or superseded by a reprocess
  cannot write results.
* At most one job per document runs at a time, follow-up jobs and remote-index
  purges included, and at most one purge runs at a time overall (LightRAG
  deletes serially and answers "busy" otherwise).
* A running job is kept fresh by a heartbeat; a job whose heartbeat stopped
  (worker crashed or was restarted) is put back in the queue by
  ``sweep_stale_jobs``.

Lock order: a transaction that writes a Knowledge Base's graph takes
``lock_knowledge_base_graph`` before it writes any document row.  A hand-over
of a running job (sweep, reprocess, delete) goes through ``release_if_stale``,
which locks the document row before the job row and never waits for either;
the worker's commit guard waits only for its own job row.
"""
from __future__ import annotations

import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Callable, Iterator

from sqlalchemy import event, exists, text
from sqlalchemy.orm import Query, Session, aliased

from .config import get_settings
from .models import Document, GraphProjectionEvent, JobType, ProcessingJob
from .provider_holds import held_job_types

logger = logging.getLogger(__name__)

# Follow-up stages run after a document is already searchable and are cheap;
# running them before new full pipelines keeps a legal document's registry
# extraction from queueing minutes behind freshly uploaded bulk work (F4).
FOLLOW_UP_JOB_TYPES = (JobType.EXTRACT_LEGAL_METADATA, JobType.REINDEX_EMBEDDINGS, JobType.EXTRACT_DOCUMENT_METADATA)
DOCUMENT_PIPELINE_JOB_TYPES = frozenset({JobType.PROCESS_DOCUMENT, JobType.REPROCESS_DOCUMENT, JobType.RESTORE_DOCUMENT,
                                         JobType.AUTO_RETRY_DOCUMENT})
# How many queued rows one claim attempt looks at (and briefly locks) per tier.
CLAIM_SCAN_LIMIT = 25
# A job whose worker disappeared this many times stops being re-queued.
STALE_MAX_LOSSES = 3
# A graph event re-queued this many times by the sweep is given up.
GRAPH_EVENT_MAX_ATTEMPTS = 8
WORKER_LOST = "WORKER_LOST"

# Advisory-lock namespaces (first key of the two-int form).
_LOCK_DOCUMENT = 7410
_LOCK_KNOWLEDGE_BASE_JOB = 7411
_LOCK_KNOWLEDGE_BASE_GRAPH = 7412
_LOCK_SINGLETON = 7413

# Cleanup work (remote-index purges) yields to user-facing jobs: a purge sitting
# in the queue must not take a worker while real documents wait.
_TIERS: tuple[Callable[[Query], Query], ...] = (
    lambda query: query.filter(ProcessingJob.job_type.in_(FOLLOW_UP_JOB_TYPES)),
    lambda query: query.filter(ProcessingJob.job_type != JobType.PURGE_REMOTE_INDEX),
    lambda query: query,
)


class ClaimLost(RuntimeError):
    """The job no longer belongs to this worker (swept as stale or superseded)."""


def is_postgres(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def _try_xact_lock(db: Session, namespace: int, key: str) -> bool:
    if not is_postgres(db):
        return True
    return bool(db.execute(text("SELECT pg_try_advisory_xact_lock(:ns, hashtext(:key))"), {"ns": namespace, "key": key}).scalar())


def lock_knowledge_base_graph(db: Session, knowledge_base_id: str | None) -> None:
    """Serialize graph/registry writes inside one Knowledge Base until the current transaction ends.

    Parallel workers may process documents of the same Knowledge Base; their
    slow stages (extraction, embeddings, LightRAG) overlap freely, but the
    short graph-sync sections that upsert shared entities run one at a time.
    Call it before the transaction writes any document row (lock order
    Knowledge Base -> document), and never around remote HTTP calls.
    """
    if knowledge_base_id and is_postgres(db):
        db.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"), {"ns": _LOCK_KNOWLEDGE_BASE_GRAPH, "key": knowledge_base_id})


def try_singleton(db: Session, name: str) -> bool:
    """True when this process may run a periodic sweep; the lock lasts until the transaction ends."""
    return _try_xact_lock(db, _LOCK_SINGLETON, name)


def _scope_is_free(db: Session, job: ProcessingJob) -> bool:
    """Take the job's scope lock and confirm nothing else of that scope is running."""
    if job.job_type == JobType.PURGE_REMOTE_INDEX:
        if not _try_xact_lock(db, _LOCK_SINGLETON, "remote-purge"):
            return False
        other_purge = db.query(ProcessingJob.id).filter(
            ProcessingJob.job_type == JobType.PURGE_REMOTE_INDEX, ProcessingJob.status == "running", ProcessingJob.id != job.id,
        ).first()
        if other_purge is not None:
            return False
    if job.document_id:
        if not _try_xact_lock(db, _LOCK_DOCUMENT, job.document_id):
            return False
        busy = db.query(ProcessingJob.id).filter(
            ProcessingJob.document_id == job.document_id, ProcessingJob.status == "running", ProcessingJob.id != job.id,
        ).first()
        return busy is None
    if job.knowledge_base_id:
        if not _try_xact_lock(db, _LOCK_KNOWLEDGE_BASE_JOB, f"{job.job_type}:{job.knowledge_base_id}"):
            return False
        busy = db.query(ProcessingJob.id).filter(
            ProcessingJob.knowledge_base_id == job.knowledge_base_id, ProcessingJob.document_id.is_(None),
            ProcessingJob.job_type == job.job_type, ProcessingJob.status == "running", ProcessingJob.id != job.id,
        ).first()
        return busy is None
    return True


def claim_next_job(db: Session) -> ProcessingJob | None:
    """Atomically move the next runnable job to ``running`` with a new claim token and return it.

    Safe with any number of workers: rows another worker is claiming are
    skipped, and a job whose document already has a running job waits.
    """
    postgres = is_postgres(db)
    held = held_job_types(db)
    running = aliased(ProcessingJob)
    document_busy = exists().where(running.document_id == ProcessingJob.document_id, running.status == "running")
    for tier in _TIERS:
        # Jobs of documents that are already busy are filtered out in SQL so a
        # bulk of blocked follow-ups cannot fill the scan window (starvation).
        query = tier(db.query(ProcessingJob).filter(
            ProcessingJob.status == "queued", ProcessingJob.next_attempt_at <= datetime.utcnow(), ~document_busy,
            # A job whose provider is held waits in the queue untouched until the hold is lifted.
            ProcessingJob.job_type.notin_(held),
        )).order_by(ProcessingJob.created_at).limit(CLAIM_SCAN_LIMIT)
        if postgres:
            query = query.with_for_update(skip_locked=True, of=ProcessingJob)
        for job in query.all():
            if _scope_is_free(db, job):
                job.status, job.current_stage, job.claim_token = "running", "claimed", str(uuid.uuid4())
                db.commit()
                return job
        db.rollback()
    return None


def claim_next_graph_event(db: Session) -> GraphProjectionEvent | None:
    query = db.query(GraphProjectionEvent).filter(
        GraphProjectionEvent.status == "queued", GraphProjectionEvent.next_attempt_at <= datetime.utcnow(),
    ).order_by(GraphProjectionEvent.created_at)
    if is_postgres(db):
        query = query.with_for_update(skip_locked=True)
    event_row = query.first()
    if not event_row:
        db.rollback()
        return None
    event_row.status, event_row.attempt_count = "running", event_row.attempt_count + 1
    db.commit()
    return event_row


# ---------------------------------------------------------------- ownership
_OWNED = "softnix_owned_job"


def _check_ownership(session: Session) -> None:
    owned = session.info.get(_OWNED)
    if not owned:
        return
    job_id, token = owned
    lock = " FOR UPDATE" if session.get_bind().dialect.name == "postgresql" else ""
    # Compare the token only: this transaction may itself have flushed the
    # job's final status.  Every hand-over (sweep, reprocess, delete) clears
    # or replaces the token.
    row = session.connection().execute(text(f"SELECT claim_token FROM processing_jobs WHERE id = :id{lock}"), {"id": job_id}).first()
    if row is None or token is None or row[0] != token:
        raise ClaimLost(job_id)


@event.listens_for(Session, "before_commit")
def _guard_owned_commit(session: Session) -> None:
    _check_ownership(session)


@contextmanager
def own_job(db: Session, job: ProcessingJob) -> Iterator[None]:
    """Guard every commit of ``db`` with the job's claim token while the block runs."""
    previous = db.info.get(_OWNED)
    db.info[_OWNED] = (job.id, job.claim_token)
    try:
        yield
    finally:
        if previous is None:
            db.info.pop(_OWNED, None)
        else:
            db.info[_OWNED] = previous


def disown(job: ProcessingJob) -> None:
    """Take a job away from whichever worker holds it (sweep, reprocess)."""
    job.claim_token = None


# ---------------------------------------------------------------- stale jobs
def stale_cutoff(now: datetime | None = None) -> datetime:
    return (now or datetime.utcnow()) - timedelta(seconds=get_settings().job_stale_after_seconds)


def is_stale(job: ProcessingJob, now: datetime | None = None) -> bool:
    return job.status == "running" and job.updated_at is not None and job.updated_at < stale_cutoff(now)


def release_stale_job(db: Session, job: ProcessingJob) -> str:
    """Put a job whose worker disappeared back in the queue, or fail it after repeated losses."""
    disown(job)
    job.lost_count = (job.lost_count or 0) + 1
    doc = db.get(Document, job.document_id) if job.document_id else None
    if job.document_id and (doc is None or not doc.is_live) and job.job_type != JobType.PURGE_REMOTE_INDEX:
        job.status, job.current_stage, job.error_code = "cancelled", "cancelled", "DOCUMENT_DELETED"
        return "cancelled"
    pipeline = job.job_type in DOCUMENT_PIPELINE_JOB_TYPES
    metadata = job.job_type == JobType.EXTRACT_DOCUMENT_METADATA
    if job.lost_count >= STALE_MAX_LOSSES:
        job.status, job.current_stage = "failed", "failed"
        job.error_code, job.error_message = WORKER_LOST, "The worker stopped while running this job too many times."
        if doc is not None and pipeline:
            doc.status, doc.error_code, doc.error_message = "failed", WORKER_LOST, "Processing was interrupted repeatedly; press Process to try again."
        if doc is not None and metadata:
            doc.metadata_status = "failed"
        return "failed"
    job.status, job.current_stage, job.next_attempt_at = "queued", "retry_wait", datetime.utcnow()
    job.error_code, job.error_message = WORKER_LOST, "The worker stopped while running this job; it was re-queued."
    if doc is not None and pipeline:
        doc.status = "queued"
    if doc is not None and metadata:
        doc.metadata_status = "queued"
    return "queued"


def _lock_row(db: Session, model, row_id: str):
    """The row locked for update, or None when another transaction holds it (or it is gone)."""
    return db.query(model).filter_by(id=row_id).populate_existing().with_for_update(skip_locked=True).one_or_none()


def release_if_stale(db: Session, job_id: str, now: datetime | None = None) -> str | None:
    """Hand over a running job whose worker disappeared; None when it is live, held or gone.

    Every hand-over comes through here.  The document row is locked before the
    job row, and neither lock is waited for: a held row belongs to a live
    transaction (the worker's own commit, or another hand-over), so the job is
    left alone this round.  The worker's commit guard waits only for the job
    row, so no cycle can form between a worker and a hand-over.
    """
    db.flush()  # the caller's pending document writes (a delete) take their row lock first
    document_id = db.query(ProcessingJob.document_id).filter_by(id=job_id).scalar()
    if document_id and _lock_row(db, Document, document_id) is None and db.query(Document.id).filter_by(id=document_id).scalar():
        return None
    job = _lock_row(db, ProcessingJob, job_id)
    if job is None or not is_stale(job, now):
        return None
    return release_stale_job(db, job)


def sweep_stale_jobs(db: Session, now: datetime | None = None) -> int:
    """Re-queue running jobs and graph events whose heartbeat stopped. Returns how many were released."""
    if not try_singleton(db, "stale-job-sweep"):
        db.rollback()
        return 0
    cutoff = stale_cutoff(now)
    stale = db.query(ProcessingJob.id).filter(ProcessingJob.status == "running", ProcessingJob.updated_at < cutoff).all()
    events = db.query(GraphProjectionEvent).filter(GraphProjectionEvent.status == "running", GraphProjectionEvent.updated_at < cutoff)
    if is_postgres(db):
        events = events.with_for_update(skip_locked=True)
    released = 0
    for (job_id,) in stale:
        outcome = release_if_stale(db, job_id, now)
        if outcome is None:
            continue
        released += 1
        logger.warning("released a stale running job", extra={"job_id": job_id, "outcome": outcome})
    for event_row in events.all():
        if event_row.attempt_count >= GRAPH_EVENT_MAX_ATTEMPTS:
            event_row.status, event_row.last_error = "failed", WORKER_LOST
        else:
            event_row.status, event_row.next_attempt_at, event_row.last_error = "queued", datetime.utcnow(), WORKER_LOST
        released += 1
    db.commit()
    return released


# ---------------------------------------------------------------- heartbeat
def _beat(bind, job_id: str, token: str | None) -> None:
    with bind.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text("UPDATE processing_jobs SET updated_at = :now WHERE id = :id AND status = 'running' AND claim_token = :token"),
                           {"now": datetime.utcnow(), "id": job_id, "token": token})


@contextmanager
def job_heartbeat(db: Session, job_id: str, interval: float | None = None, token: str | None = None) -> Iterator[None]:
    """Refresh the job's ``updated_at`` from a side thread while the job runs.

    Long stages (OCR, waiting on LightRAG) can go many minutes without writing
    the job row; the heartbeat lets ``sweep_stale_jobs`` tell a slow job from a
    job whose worker is gone.  Only the current claim is refreshed.
    PostgreSQL only: SQLite tests run without it.
    """
    interval = interval if interval is not None else get_settings().job_heartbeat_seconds
    if not is_postgres(db) or interval <= 0:
        yield
        return
    if token is None:
        token = db.query(ProcessingJob.claim_token).filter_by(id=job_id).scalar()
    bind = db.get_bind()
    stop = threading.Event()

    def run() -> None:
        while not stop.wait(interval):
            try:
                _beat(bind, job_id, token)
            except Exception:  # a missed beat is harmless; the next one retries
                logger.debug("job heartbeat failed", exc_info=True, extra={"job_id": job_id})

    thread = threading.Thread(target=run, name=f"job-heartbeat-{job_id[:8]}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=10)
