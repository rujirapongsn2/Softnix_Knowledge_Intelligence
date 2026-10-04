"""Re-queue documents that failed only because the retrieval engine was unreachable."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import Document, JobType, ProcessingJob
from .retrieval import LightRAGRetrievalEngine

logger = logging.getLogger(__name__)

MAX_AUTO_RECOVERIES = 3


def recover_unavailable_documents(db: Session, engine: LightRAGRetrievalEngine, *, limit: int = 5, max_age_days: int = 30) -> int:
    """Queue documents that failed only because the engine was unreachable, once it is back.

    The worker's own retries give up after about half an hour; a longer outage then
    leaves healthy documents stuck in ``failed``. When LightRAG answers its health
    check again this re-queues them, a few at a time (so recovery cannot flood the
    engine) and at most ``MAX_AUTO_RECOVERIES`` times per document (so a document
    that keeps failing is eventually left for a person to look at).
    """
    if not engine.is_healthy():
        return 0
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    candidates = db.query(Document).filter(
        Document.status == "failed", Document.error_code == "RETRIEVAL_ENGINE_UNAVAILABLE",
        Document.live(), Document.updated_at >= cutoff,
    ).order_by(Document.updated_at.asc()).all()
    queued = 0
    for doc in candidates:
        if queued >= limit:
            break
        busy = db.query(ProcessingJob.id).filter(
            ProcessingJob.document_id == doc.id, ProcessingJob.status.in_(["queued", "running"])).first()
        attempts = db.query(ProcessingJob.id).filter_by(document_id=doc.id, job_type=JobType.AUTO_RETRY_DOCUMENT).count()
        if busy or attempts >= MAX_AUTO_RECOVERIES:
            continue
        doc.status, doc.error_code, doc.error_message = "queued", None, None
        db.add(ProcessingJob(document_id=doc.id, knowledge_base_id=doc.knowledge_base_id, job_type=JobType.AUTO_RETRY_DOCUMENT))
        queued += 1
        logger.info("re-queued a document that failed while the retrieval engine was unavailable", extra={"document_id": doc.id})
    if queued:
        db.commit()
    return queued
