"""Ingest pre-extracted text (JSON) — the InsightDOC Custom API path.

Machine-to-machine senders that already hold extracted text (OCR/LLM
pipelines such as InsightDOC) have no original binary to upload: their
Custom API nodes speak JSON only. This module wraps the text as a stored
Markdown file so the regular processing pipeline (chunking, embedding,
LightRAG, legal extraction) runs unchanged.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Document, ProcessingJob

MAX_TEXT_BYTES = 20 * 1024 * 1024  # 20 MB of text is far beyond any sane document


class _TextUpload:
    """Duck-typed stand-in for Starlette's UploadFile as consumed by
    ``store_upload``/``create_document_job`` (filename/content_type/read)."""

    def __init__(self, filename: str, text: str):
        self.filename = filename
        self.content_type = "text/markdown"
        self._data = text.encode("utf-8")

    async def read(self) -> bytes:
        return self._data

    def sync_read(self) -> bytes:
        return self._data


def _safe_stem(title: str) -> str:
    keep = [ch if (ch.isalnum() or ch in {"-", "_", " ", ".", "(", ")"}) else "_" for ch in title]
    stem = "".join(keep).strip() or "document"
    return stem[:120]


def create_text_document_job(db: Session, knowledge_base_id: str, title: str, text: str,
                             document_type: str = "general", published_at: date | None = None,
                             metadata_template: dict | None = None,
                             document_metadata: dict | None = None, external_id: str | None = None) -> tuple[Document, ProcessingJob]:
    """Store extracted text as a Markdown document and queue processing.

    Mirrors ``create_document_job`` (type validation, storage, checksum
    dedup within the KB, metadata search text) without requiring a binary
    upload — the text is the artifact.
    """
    from .services import DOCUMENT_TYPES, DocumentExists, document_exists_error, live_document_id, normalize_external_id  # local import keeps module import graph flat

    if document_type not in DOCUMENT_TYPES:
        raise ValueError("DOCUMENT_TYPE_INVALID")
    if not text.strip():
        raise ValueError("TEXT_EMPTY")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("FILE_TOO_LARGE")
    external_id = normalize_external_id(external_id)
    if external_id and (existing := live_document_id(db, knowledge_base_id, external_id=external_id)):
        raise DocumentExists("EXTERNAL_ID_EXISTS", existing)

    settings = get_settings()
    root = settings.file_root / knowledge_base_id
    root.mkdir(parents=True, exist_ok=True)
    extension = ".md"
    stored_name = f"{uuid.uuid4()}{extension}"
    path = root / stored_name
    payload = text.encode("utf-8")
    path.write_bytes(payload)

    checksum = hashlib.sha256(payload).hexdigest()
    if duplicate := live_document_id(db, knowledge_base_id, checksum=checksum):
        path.unlink(missing_ok=True)
        raise DocumentExists("FILE_DUPLICATE", duplicate)

    doc = Document(
        knowledge_base_id=knowledge_base_id, external_id=external_id,
        original_filename=f"{_safe_stem(title)}.md",
        storage_path=str(path), stored_filename=stored_name, file_size=len(payload),
        mime_type="text/markdown", checksum_sha256=checksum,
        title=title or f"{_safe_stem(title)}.md",
        document_type=document_type, published_at=published_at,
        metadata_template_id=(metadata_template or {}).get("id"),
        metadata_template_name=(metadata_template or {}).get("name"),
        metadata_template_version=(metadata_template or {}).get("version"),
        metadata_template_fields=(metadata_template or {}).get("fields", []),
        document_metadata=document_metadata or {},
    )
    if document_metadata:
        from .services import metadata_search_text
        doc.metadata_search_text = metadata_search_text(
            (metadata_template or {}).get("fields", []), document_metadata
        )
    db.add(doc)
    try:
        db.flush()
    except IntegrityError as exc:
        # Two concurrent sends of the same text or external_id: the pre-checks
        # above can miss the race, the unique indexes are the real guard.
        # Surface it as the same 409 a retrying client sees from the
        # non-concurrent path instead of an unhandled 500.
        db.rollback()
        path.unlink(missing_ok=True)
        if exists := document_exists_error(db, knowledge_base_id, str(exc), checksum, external_id):
            raise exists from None
        raise
    job = ProcessingJob(document_id=doc.id, knowledge_base_id=knowledge_base_id)
    db.add(job)
    db.commit()
    db.refresh(doc)
    db.refresh(job)
    return doc, job
