"""Metadata export: the document types of a Knowledge Base and each document's metadata values, as one JSON file.

A document keeps the field snapshot of the type version it was described with, so a type appears once per
version that documents use, and each document points at its (id, version). A document-level export is the same
shape holding one document.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session, load_only

from .document_templates import list_templates
from .metadata_extraction import field_review_states
from .models import Document, KnowledgeBase

EXPORT_SCHEMA_VERSION = "ski.metadata-export.v1"


def _type_ref(document: Document) -> tuple[str, int]:
    return document.metadata_template_id or "system:" + document.document_type, document.metadata_template_version or 1


def _document_view(document: Document) -> dict:
    template_id, version = _type_ref(document)
    return {
        "id": document.id, "external_id": document.external_id, "title": document.title,
        "original_filename": document.original_filename, "status": document.status,
        "document_type": {"id": template_id, "version": version},
        "metadata_status": document.metadata_status, "metadata_revision": document.metadata_revision,
        "metadata": document.document_metadata or {},
        "field_states": field_review_states(document.metadata_template_fields, document.document_metadata,
                                            document.metadata_observations),
        "updated_at": document.updated_at.isoformat() if document.updated_at else None,
    }


def _document_types(db: Session, kb_id: str, documents: list[Document], *, include_unused: bool) -> list[dict]:
    current = {template["id"]: template for template in list_templates(db, kb_id, include_inactive=True)}
    types: dict[tuple[str, int], dict] = {}
    # A rename keeps the version, and system types stay at version 1 when their fields change in code, so the
    # current definition may differ from what documents were filled in with: their saved copy wins.
    for document in documents:
        ref = _type_ref(document)
        if ref in types:
            continue
        template = current.get(ref[0], {})
        types[ref] = {**template, "id": ref[0], "version": ref[1],
                      "name": document.metadata_template_name or template.get("name"),
                      "fields": document.metadata_template_fields or []}
    if include_unused:
        for template in current.values():
            types.setdefault((template["id"], template["version"]), template)
    keys = ("id", "code", "name", "description", "base_document_type", "version", "is_system", "is_active", "fields")
    return [{**{key: item.get(key) for key in keys}, "is_current": current.get(ref[0], {}).get("version") == ref[1]}
            for ref, item in types.items()]


def metadata_export(db: Session, kb: KnowledgeBase, document_id: str | None = None) -> dict:
    """Export every live document of ``kb``, or only ``document_id``, with the document types they use."""
    query = db.query(Document).options(load_only(
        Document.id, Document.external_id, Document.title, Document.original_filename, Document.status,
        Document.document_type, Document.metadata_template_id, Document.metadata_template_name,
        Document.metadata_template_version, Document.metadata_template_fields, Document.document_metadata,
        Document.metadata_observations, Document.metadata_status, Document.metadata_revision, Document.updated_at,
    )).filter(Document.knowledge_base_id == kb.id, Document.live())
    if document_id:
        query = query.filter(Document.id == document_id)
    documents = query.order_by(Document.created_at, Document.id).all()
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "scope": "document" if document_id else "knowledge_base",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "knowledge_base": {"id": kb.id, "code": kb.code, "name": kb.name},
        "document_types": _document_types(db, kb.id, documents, include_unused=not document_id),
        "documents": [_document_view(document) for document in documents],
    }
