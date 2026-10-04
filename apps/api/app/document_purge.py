"""Permanently remove the content of documents that were already deleted.

Deleting a document is a soft delete so it can be restored. Purging removes what
a restore would need (the stored files, chunks, extracted text) together with the
graph facts that only that document supported. The ``documents`` row is kept as a
tombstone with ``purged_at`` set, so audit and job history stay intact, foreign
keys stay valid, and ``POST /documents/{id}/restore`` can refuse with a clear
``DOCUMENT_PURGED`` instead of re-processing a file that no longer exists.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .config import get_settings
from .graph_retirement import drop_document_evidence
from .models import Document, DocumentChunk, DocumentLifecycle, DocumentMetadataValue, JobType, LegalInstrument, LegalInstrumentRelation, ProcessingJob
from .remote_index import purge_remote_record
from .retrieval import LightRAGRetrievalEngine

logger = logging.getLogger(__name__)

DEFAULT_MIN_AGE_DAYS = 30


class PurgeBlocked(Exception):
    """The document cannot be purged yet; the message says why."""


def _stored_files(document: Document) -> list[Path]:
    """Files this document owns on disk, limited to the storage root."""
    root = get_settings().file_root.resolve()
    files = []
    for raw in (document.storage_path, document.reference_file_path):
        if not raw:
            continue
        path = Path(raw).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            logger.warning("refusing to delete a file outside the storage root", extra={"document_id": document.id, "path": str(path)})
            continue
        files.append(path)
    return files


def _check_purgeable(db: Session, document: Document, engine: LightRAGRetrievalEngine | None) -> None:
    in_flight = db.query(ProcessingJob.id).filter(
        ProcessingJob.document_id == document.id, ProcessingJob.status.in_(["queued", "running"]),
        ProcessingJob.job_type != JobType.PURGE_REMOTE_INDEX,
    ).first()
    if in_flight:
        raise PurgeBlocked("a processing job for this document is still queued or running")
    if engine is not None and engine.enabled:
        purge_remote_record(engine, document)  # the remote purge job may still be waiting behind user work


def purge_document(db: Session, document: Document, *, engine: LightRAGRetrievalEngine | None = None) -> dict[str, Any]:
    """Permanently remove one deleted document's content. Safe to run again."""
    files = _stored_files(document)
    if document.lifecycle is DocumentLifecycle.LIVE:
        raise PurgeBlocked("document is not deleted")
    if document.lifecycle is DocumentLifecycle.DELETED:
        _check_purgeable(db, document, engine)
        instrument_ids = [row[0] for row in db.query(LegalInstrument.id).filter_by(document_id=document.id)]
        if instrument_ids:
            db.query(LegalInstrumentRelation).filter(or_(
                LegalInstrumentRelation.source_instrument_id.in_(instrument_ids),
                LegalInstrumentRelation.target_instrument_id.in_(instrument_ids),
            )).delete(synchronize_session=False)
            db.query(LegalInstrument).filter(LegalInstrument.id.in_(instrument_ids)).delete(synchronize_session=False)
        graph = drop_document_evidence(db, document.id)
        chunks = db.query(DocumentChunk).filter_by(document_id=document.id).delete(synchronize_session=False)
        db.query(DocumentMetadataValue).filter_by(document_id=document.id).delete(synchronize_session=False)
        document.extracted_text = None
        document.purged_at = datetime.utcnow()
        db.commit()
    else:
        graph, chunks = {"entities_retired": 0, "relationships_retired": 0}, 0
    # Files go last: if removing one fails the database is already consistent and a
    # rerun (purged_at set) only retries the files.
    removed_files, failed_files = [], []
    for path in files:
        if not path.exists():
            continue
        try:
            path.unlink()
            removed_files.append(str(path))
        except OSError:
            logger.exception("could not remove a purged document's file", extra={"document_id": document.id, "path": str(path)})
            failed_files.append(str(path))
    return {"document_id": document.id, "title": document.title, "chunks_removed": chunks, "files_removed": removed_files, "files_failed": failed_files, **graph}


def purgeable_documents(db: Session, *, knowledge_base_id: str | None = None, min_age_days: int = DEFAULT_MIN_AGE_DAYS) -> list[Document]:
    cutoff = datetime.utcnow() - timedelta(days=min_age_days)
    query = db.query(Document).filter(Document.restorable(), Document.deleted_at <= cutoff)
    if knowledge_base_id:
        query = query.filter(Document.knowledge_base_id == knowledge_base_id)
    return query.order_by(Document.deleted_at).all()


def purge_deleted_documents(db: Session, *, knowledge_base_id: str | None = None, min_age_days: int = DEFAULT_MIN_AGE_DAYS,
                            apply: bool = False, engine: LightRAGRetrievalEngine | None = None) -> dict[str, Any]:
    """Report (and with ``apply=True`` purge) deleted documents older than ``min_age_days``."""
    candidates = purgeable_documents(db, knowledge_base_id=knowledge_base_id, min_age_days=min_age_days)
    report: dict[str, Any] = {"applied": apply, "min_age_days": min_age_days, "knowledge_base_id": knowledge_base_id,
                              "candidates": [{"document_id": doc.id, "title": doc.title, "deleted_at": doc.deleted_at,
                                              "files": [str(path) for path in _stored_files(doc)]} for doc in candidates],
                              "purged": [], "blocked": []}
    if not apply:
        return report
    for doc in candidates:
        try:
            report["purged"].append(purge_document(db, doc, engine=engine))
        except (PurgeBlocked, RuntimeError) as exc:
            db.rollback()
            report["blocked"].append({"document_id": doc.id, "title": doc.title, "reason": str(exc)})
    return report


if __name__ == "__main__":  # python -m app.document_purge [--kb ID] [--min-age-days N] [--apply]
    import argparse
    import json

    from .db import SessionLocal

    parser = argparse.ArgumentParser(description="Permanently purge documents that were deleted (report only unless --apply).")
    parser.add_argument("--kb", help="limit to one Knowledge Base id")
    parser.add_argument("--min-age-days", type=int, default=DEFAULT_MIN_AGE_DAYS,
                        help=f"only documents deleted at least this many days ago (default {DEFAULT_MIN_AGE_DAYS})")
    parser.add_argument("--apply", action="store_true", help="purge for real; cannot be undone")
    arguments = parser.parse_args()
    with SessionLocal() as session:
        result = purge_deleted_documents(session, knowledge_base_id=arguments.kb, min_age_days=arguments.min_age_days,
                                         apply=arguments.apply, engine=LightRAGRetrievalEngine())
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
