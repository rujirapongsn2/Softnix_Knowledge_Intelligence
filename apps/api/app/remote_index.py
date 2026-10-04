"""Keep LightRAG's source registry consistent with the platform's documents.

LightRAG deduplicates inserts by content hash. A document that was deleted on
the platform but whose remote record survived (e.g. the engine was down when
the purge job ran) therefore blocks any re-upload of identical content with a
bare ``RETRIEVAL_ENGINE_REJECTED``. This module resolves that collision and
repairs the leftovers.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from .models import Document, KnowledgeBase
from .retrieval import LightRAGRetrievalEngine, RetrievalEngineError, duplicate_original_id

logger = logging.getLogger(__name__)

# Records still being worked on by LightRAG are never touched by repairs.
_IN_FLIGHT_STATUSES = {"pending", "parsing", "analyzing", "processing", "preprocessed"}


def _is_orphan(document: Document | None) -> bool:
    return document is None or not document.is_live


class HealOutcome(StrEnum):
    CLEARED = "cleared"
    ALREADY_INDEXED = "already_indexed"


def resolve_remote_duplicate(db: Session, engine: LightRAGRetrievalEngine, doc: Document, error: str | None) -> HealOutcome:
    """Handle LightRAG's "identical content already exists" rejection for ``doc``.

    Returns ``ALREADY_INDEXED`` when the colliding record is this very document
    (nothing to ingest) or ``CLEARED`` when a stale record was removed and the
    caller should ingest again. Raises ``RETRIEVAL_ENGINE_DUPLICATE`` when the
    content genuinely lives on under another live document, or when the owner of
    the colliding record cannot be established (unknown records are never deleted).
    """
    original_id = duplicate_original_id(error)
    original = engine.get_remote_document(original_id) if original_id else None
    if original is None:
        if original_id:
            # The colliding record vanished between the insert and this lookup.
            return HealOutcome.CLEARED
        raise RetrievalEngineError("RETRIEVAL_ENGINE_DUPLICATE", error or "Identical content already exists.")
    owner_id = original.get("document_id")
    if owner_id == doc.id:
        if original.get("status") == "processed":
            return HealOutcome.ALREADY_INDEXED
        raise RetrievalEngineError("RETRIEVAL_ENGINE_DUPLICATE", error)
    if not owner_id:
        raise RetrievalEngineError("RETRIEVAL_ENGINE_DUPLICATE", f"{error} (the existing record is not managed by this platform)")
    owner = db.get(Document, owner_id)
    if not _is_orphan(owner):
        raise RetrievalEngineError(
            "RETRIEVAL_ENGINE_DUPLICATE",
            f"Identical content is already indexed for the live document \"{owner.title}\" ({owner.id}). "
            "Delete that document first, or upload different content.")
    logger.warning("removing stale remote record that blocks a re-upload", extra={
        "document_id": doc.id, "stale_remote_id": original["id"], "stale_document_id": owner_id})
    _delete_unless_gone(engine, original["id"])
    # The rejected insert left its own FAILED record under this document's label.
    purge_remote_record(engine, doc, only_status="failed")
    return HealOutcome.CLEARED


def purge_remote_record(engine: LightRAGRetrievalEngine, doc: Document, *, only_status: str | None = None) -> bool:
    """Delete this document's record from the retrieval index. Returns whether a record was deleted.

    The one place that finds and removes a document's remote record: the purge job, the permanent
    purge and duplicate healing all go through it.
    """
    record = engine.find_document(doc.id, doc.knowledge_base_id)
    if not record or (only_status and str(record.get("status") or "").lower() != only_status):
        return False
    _delete_unless_gone(engine, record["id"])
    return True


def _delete_unless_gone(engine: LightRAGRetrievalEngine, remote_id: str) -> None:
    """Delete a remote record, tolerating one that a queued purge job removed first.

    BUSY and other transient states still propagate so the worker retries the job.
    """
    try:
        engine.delete_remote_document(remote_id)
    except RuntimeError as exc:
        if str(exc) == "RETRIEVAL_ENGINE_BUSY" or engine.get_remote_document(remote_id) is not None:
            raise


@dataclass
class OrphanRecord:
    remote_id: str
    status: str
    knowledge_base_id: str
    document_id: str
    title: str
    deleted: bool


def find_orphan_remote_documents(db: Session, engine: LightRAGRetrievalEngine) -> list[OrphanRecord]:
    """Remote records whose platform document is deleted or no longer exists.

    A LightRAG server can be shared with another environment that uses the same
    label format, so a record only counts when its Knowledge Base belongs to this
    platform's database; anything else is foreign and left alone.
    """
    known_knowledge_bases = {kb_id for (kb_id,) in db.query(KnowledgeBase.id).all()}
    orphans: list[OrphanRecord] = []
    for row in engine.list_remote_documents():
        document_id = row.get("document_id")
        if not document_id or row.get("status") in _IN_FLIGHT_STATUSES or row.get("knowledge_base_id") not in known_knowledge_bases:
            continue
        document = db.get(Document, document_id)
        if _is_orphan(document):
            orphans.append(OrphanRecord(remote_id=str(row["id"]), status=str(row.get("status")),
                                        knowledge_base_id=str(row.get("knowledge_base_id")), document_id=document_id,
                                        title=str(row.get("title") or ""), deleted=document is not None))
    return orphans


def purge_orphan_remote_documents(db: Session, engine: LightRAGRetrievalEngine, *, apply: bool = False,
                                  busy_retries: int = 24, busy_wait: float = 5.0) -> dict[str, Any]:
    """Report (and with ``apply=True`` delete) orphaned remote records.

    LightRAG removes one source at a time and answers BUSY while it rebuilds the
    graph after a deletion, so BUSY is retried with a pause. One failure never
    stops the rest; leftovers are reported so the sweep can simply be run again.
    """
    orphans = find_orphan_remote_documents(db, engine)
    removed, failed = [], []
    if apply:
        for orphan in orphans:
            for attempt in range(1, busy_retries + 1):
                try:
                    engine.delete_remote_document(orphan.remote_id)
                    removed.append(orphan.remote_id)
                    break
                except RuntimeError as exc:
                    if str(exc) == "RETRIEVAL_ENGINE_BUSY" and attempt < busy_retries:
                        time.sleep(busy_wait)
                        continue
                    failed.append({"remote_id": orphan.remote_id, "error": str(exc)})
                    break
    return {"applied": apply, "found": len(orphans), "removed": removed, "failed": failed,
            "orphans": [orphan.__dict__ for orphan in orphans]}


if __name__ == "__main__":  # python -m app.remote_index [--apply]
    import argparse
    import json

    from .db import SessionLocal

    parser = argparse.ArgumentParser(description="List (and optionally delete) LightRAG records whose platform document is gone.")
    parser.add_argument("--apply", action="store_true", help="delete the orphaned records (default: report only)")
    arguments = parser.parse_args()
    with SessionLocal() as session:
        print(json.dumps(purge_orphan_remote_documents(session, LightRAGRetrievalEngine(), apply=arguments.apply), ensure_ascii=False, indent=2, default=str))
