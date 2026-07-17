import hashlib
import logging
import mimetypes
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup
from docx import Document as WordDocument
import httpx
from markitdown import MarkItDown
from pypdf import PdfReader
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .config import get_settings
from .db import SessionLocal
from .external_ocr import ExternalOcrClient
from .graph_store import Neo4jGraphStore
from .legal_registry import AUTHORITY_LEVELS, classify_kind, normalize_family_key, parse_provision_refs, parse_thai_date, provision_number_matches, resolve_instrument_statuses
from .legal_resolver import resolve_legal_context
from .legal_corpus import parse_legal_corpus_metadata
from .models import Document, DocumentChunk, Entity, EntitySource, GraphProjectionEvent, KnowledgeBase, LegalFamily, LegalInstrument, LegalInstrumentRelation, ProcessingJob, QueryResult, Relationship, RelationshipSource
from .openrouter import OpenRouterClient
from .observability import metrics
from .planner import LegalContext, RetrievalChannel, RetrievalPlan, PlannerDecision, apply_llm_plan, intersect_policies, policy_from_config, rule_plan
from .retrieval import LightRAGRetrievalEngine, RetrievalEvidence

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".txt", ".md", ".html", ".htm", ".csv", ".json"}
LEGACY_EXTRACTOR_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".html", ".htm", ".csv", ".json"}
ALLOWED_MIME_TYPES = {
    ".pdf": {"application/pdf"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".xls": {"application/vnd.ms-excel"},
    ".txt": {"text/plain"}, ".md": {"text/markdown", "text/plain"},
    ".html": {"text/html"}, ".htm": {"text/html"},
    ".csv": {"text/csv", "application/csv", "text/plain"},
    ".json": {"application/json", "text/json", "text/plain"},
}
DEFAULT_RETRIEVAL_CONFIG = {
    "version": 1, "retrieval_mode": "auto", "enable_vector": True, "enable_graph": True,
    "enable_fulltext": True, "enable_lightrag": True, "enable_reranker": True,
    "planner_llm_fallback": True, "default_top_k": 12, "maximum_top_k": 30,
    "maximum_graph_depth": 3, "citation_required": True,
}
TRANSIENT_PROCESSING_ERRORS = {
    "RETRIEVAL_ENGINE_UNAVAILABLE", "RETRIEVAL_ENGINE_REJECTED", "RETRIEVAL_ENGINE_BUSY",
    "RETRIEVAL_ENGINE_TIMEOUT", "OPENROUTER_UNAVAILABLE", "EXTERNAL_OCR_UNAVAILABLE", "EXTERNAL_OCR_TIMEOUT",
}
MAX_PROCESSING_ATTEMPTS = 3
logger = logging.getLogger(__name__)


def processing_retry_delay(attempt_count: int) -> int:
    return min(60, 2 ** max(1, attempt_count))


def canonical_entity_name(value: str) -> str:
    return " ".join(value.casefold().split())


def generic_identity_key(name: str) -> str:
    return f"generic:{canonical_entity_name(name)}"


def create_entity(db: Session, knowledge_base_id: str, payload) -> Entity:
    canonical = canonical_entity_name(payload.name)
    identity_key = generic_identity_key(payload.name)
    entity = db.query(Entity).filter_by(knowledge_base_id=knowledge_base_id, identity_key=identity_key, entity_type=payload.entity_type).first()
    if not entity:
        entity = Entity(knowledge_base_id=knowledge_base_id, name=payload.name, canonical_name=canonical,
                        entity_type=payload.entity_type, description=payload.description, aliases=payload.aliases,
                        attributes=payload.attributes, confidence=payload.confidence, identity_key=identity_key,
                        origin="manual", review_status="verified", is_legal=False)
        db.add(entity)
        db.flush()
    elif entity.deleted_at:
        entity.deleted_at, entity.name, entity.description = None, payload.name, payload.description
    if payload.document_id and payload.excerpt:
        exists = db.query(EntitySource).filter_by(entity_id=entity.id, document_id=payload.document_id, excerpt=payload.excerpt).first()
        if not exists:
            db.add(EntitySource(entity_id=entity.id, document_id=payload.document_id, excerpt=payload.excerpt))
            entity.source_count += 1
    db.add(GraphProjectionEvent(event_type="entity", entity_id=entity.id))
    db.commit()
    db.refresh(entity)
    return entity


def create_relationship(db: Session, knowledge_base_id: str, payload) -> Relationship:
    source = db.get(Entity, payload.source_entity_id)
    target = db.get(Entity, payload.target_entity_id)
    if not source or not target or source.knowledge_base_id != knowledge_base_id or target.knowledge_base_id != knowledge_base_id:
        raise ValueError("ENTITY_NOT_FOUND")
    relationship = db.query(Relationship).filter_by(knowledge_base_id=knowledge_base_id, source_entity_id=source.id,
                                                     target_entity_id=target.id, relationship_type=payload.relationship_type).first()
    if not relationship:
        relationship = Relationship(knowledge_base_id=knowledge_base_id, source_entity_id=source.id, target_entity_id=target.id,
                                    relationship_type=payload.relationship_type, description=payload.description, confidence=payload.confidence,
                                    origin="manual", review_status="verified", is_legal=False)
        db.add(relationship)
        db.flush()
    elif relationship.deleted_at:
        relationship.deleted_at, relationship.description, relationship.confidence = None, payload.description, payload.confidence
    if payload.document_id and payload.excerpt:
        exists = db.query(RelationshipSource).filter_by(relationship_id=relationship.id, document_id=payload.document_id, excerpt=payload.excerpt).first()
        if not exists:
            db.add(RelationshipSource(relationship_id=relationship.id, document_id=payload.document_id, excerpt=payload.excerpt))
            relationship.source_count += 1
    db.add(GraphProjectionEvent(event_type="relationship", relationship_id=relationship.id))
    db.commit()
    db.refresh(relationship)
    return relationship


def sync_lightrag_document_graph(db: Session, document: Document, max_labels: int = 200) -> dict[str, int]:
    """Materialize document-scoped LightRAG graph evidence in the editable local graph.

    LightRAG keeps one shared graph service, so file-source provenance is the
    boundary that prevents nodes from a different Knowledge Base leaking in.
    """
    # LightRAG's opaque relationship labels are useful retrieval signals but
    # are not legal facts.  Legal documents get their graph only from the
    # evidence-backed legal projection below.
    if document.document_type in LEGAL_DOCUMENT_TYPES:
        return {"entities": 0, "relationships": 0}
    engine = LightRAGRetrievalEngine()
    if not engine.enabled:
        return {"entities": 0, "relationships": 0}
    marker = f"softnix-kb={document.knowledge_base_id}__doc={document.id}__"
    graphs: list[dict] = []
    try:
        for label in engine.graph_labels()[:max_labels]:
            graph = engine.graph(label, max_depth=1, max_nodes=100)
            if any(marker in str(node.get("properties", {}).get("file_path", "")) for node in graph.get("nodes", [])):
                graphs.append(graph)
    except RuntimeError:
        # Graph sync must never mark a successfully indexed document as failed.
        return {"entities": 0, "relationships": 0}

    raw_nodes: dict[str, dict] = {}
    raw_edges: dict[str, dict] = {}
    for graph in graphs:
        for node in graph.get("nodes", []):
            props = node.get("properties", {})
            if marker in str(props.get("file_path", "")):
                raw_nodes.setdefault(str(node.get("id") or props.get("entity_id") or ""), node)
        for edge in graph.get("edges", []):
            props = edge.get("properties", {})
            if marker in str(props.get("file_path", "")):
                raw_edges.setdefault(str(edge.get("id") or f"{edge.get('source')}->{edge.get('target')}"), edge)

    local_by_external: dict[str, Entity] = {}
    entity_count = relationship_count = 0
    for external_id, node in raw_nodes.items():
        if not external_id:
            continue
        props = node.get("properties", {})
        name = str(props.get("entity_id") or node.get("labels", [external_id])[0] or external_id).strip()
        if not name:
            continue
        entity_type = str(props.get("entity_type") or "Concept").strip()[:100] or "Concept"
        canonical = canonical_entity_name(name)
        identity_key = generic_identity_key(name)
        entity = db.query(Entity).filter_by(knowledge_base_id=document.knowledge_base_id, identity_key=identity_key, entity_type=entity_type).first()
        if not entity:
            entity = Entity(knowledge_base_id=document.knowledge_base_id, name=name, canonical_name=canonical, entity_type=entity_type,
                            description=str(props.get("description") or "")[:5000] or None, confidence=1.0,
                            identity_key=identity_key, origin="ai_suggestion", review_status="suggested", is_legal=False)
            db.add(entity); db.flush(); entity_count += 1
            db.add(GraphProjectionEvent(event_type="entity", entity_id=entity.id))
        elif entity.deleted_at:
            entity.deleted_at = None
        local_by_external[external_id] = entity
        excerpt = str(props.get("description") or name)[:5000]
        if not db.query(EntitySource).filter_by(entity_id=entity.id, document_id=document.id, excerpt=excerpt).first():
            db.add(EntitySource(entity_id=entity.id, document_id=document.id, excerpt=excerpt)); entity.source_count += 1

    for edge in raw_edges.values():
        source, target = local_by_external.get(str(edge.get("source"))), local_by_external.get(str(edge.get("target")))
        if not source or not target or source.id == target.id:
            continue
        relationship_type = "RELATED_TO"
        relationship = db.query(Relationship).filter_by(knowledge_base_id=document.knowledge_base_id, source_entity_id=source.id,
                                                         target_entity_id=target.id, relationship_type=relationship_type).first()
        props = edge.get("properties", {})
        description = str(props.get("description") or "")[:5000] or None
        if not relationship:
            relationship = Relationship(knowledge_base_id=document.knowledge_base_id, source_entity_id=source.id, target_entity_id=target.id,
                                        relationship_type=relationship_type, description=description, confidence=1.0,
                                        origin="ai_suggestion", review_status="suggested", is_legal=False)
            db.add(relationship); db.flush(); relationship_count += 1
            db.add(GraphProjectionEvent(event_type="relationship", relationship_id=relationship.id))
        elif relationship.deleted_at:
            relationship.deleted_at = None
        excerpt = description or f"{source.name} relates to {target.name}"
        if not db.query(RelationshipSource).filter_by(relationship_id=relationship.id, document_id=document.id, excerpt=excerpt).first():
            db.add(RelationshipSource(relationship_id=relationship.id, document_id=document.id, excerpt=excerpt)); relationship.source_count += 1
    db.commit()
    return {"entities": entity_count, "relationships": relationship_count}


LEGAL_GRAPH_RELATIONSHIPS = {
    "CONTAINS_PROVISION", "ISSUED_BY", "PARTY_TO", "REQUIRES", "GRANTS_RIGHT", "PROHIBITS", "DEFINES",
    "ISSUED_UNDER", "IMPLEMENTS", "AMENDS", "REPEALS", "SUPERSEDES", "REFERS_TO", "GOVERNED_BY",
}
# Cross-instrument subset of LEGAL_GRAPH_RELATIONSHIPS; the rest (CONTAINS_PROVISION,
# ISSUED_BY, PARTY_TO, ...) describe facts within a single document and never become
# a legal_instrument_relations row.
LEGAL_INSTRUMENT_RELATION_TYPES = {
    "ISSUED_UNDER", "IMPLEMENTS", "AMENDS", "REPEALS", "SUPERSEDES", "REFERS_TO", "GOVERNED_BY",
}


def _legal_value(item, *keys: str) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    return str(next((item.get(key) for key in keys if item.get(key)), "")).strip()


def _legal_evidence(item, fallback: str) -> str:
    return (_legal_value(item, "evidence_quote", "excerpt", "text", "description") or fallback)[:5000]


def _reference_confidence(reference, default: float = 0.5) -> float:
    """An explicit confidence of 0.0 is a real (low) signal, not a missing value."""
    value = reference.get("confidence") if isinstance(reference, dict) else None
    return float(value) if value is not None else default


def legal_metadata_v2(document: Document) -> dict:
    """Normalize legacy legal metadata without discarding a human-edited value."""
    metadata = document.legal_metadata or {}
    if metadata.get("schema_version") == 2:
        # Backfill official corpus identity for records created before the
        # deterministic parser was introduced, while preserving curator edits.
        parsed = parse_legal_corpus_metadata(document.extracted_text or "", document.title or document.original_filename)
        current_instrument = metadata.get("instrument") if isinstance(metadata.get("instrument"), dict) else {}
        metadata["instrument"] = {**parsed.get("instrument", {}), **current_instrument}
        if not metadata.get("change_events") and parsed.get("change_events"):
            metadata["change_events"] = parsed["change_events"]
        if not metadata.get("amendments") and parsed.get("amendments"):
            metadata["amendments"] = parsed["amendments"]
        return metadata
    kind_by_document_type = {"legal": "Act", "regulation": "Regulation", "contract": "Contract"}
    provisions = []
    for article in metadata.get("articles", []) if isinstance(metadata.get("articles"), list) else []:
        number = _legal_value(article, "number", "article_number", "label", "name", "title")
        if number:
            provisions.append({
                "kind": _legal_value(article, "kind") or "article", "number": number,
                "heading": _legal_value(article, "heading", "title"), "text": _legal_value(article, "text"),
                "evidence_quote": _legal_evidence(article, f"มาตรา {number}"),
            })
    parsed = parse_legal_corpus_metadata(document.extracted_text or "", document.title or document.original_filename)
    parsed_instrument = parsed.get("instrument", {})
    return {
        "schema_version": 2,
        # Compatibility aliases remain readable by existing clients while the
        # graph projection uses the normalized v2 fields below.
        "document_type": metadata.get("document_type"),
        "articles": metadata.get("articles") if isinstance(metadata.get("articles"), list) else [],
        "instrument": {
            "kind": kind_by_document_type.get(document.document_type, "Other"),
            "official_title": document.title or document.original_filename,
            "official_number": metadata.get("document_number"),
            "jurisdiction": metadata.get("jurisdiction"),
            "effective_date": metadata.get("effective_date"),
            "issuer": metadata.get("issuer"),
            **parsed_instrument,
        },
        "provisions": provisions,
        "parties": metadata.get("parties") if isinstance(metadata.get("parties"), list) else [],
        "obligations": metadata.get("obligations") if isinstance(metadata.get("obligations"), list) else [],
        "rights": metadata.get("rights") if isinstance(metadata.get("rights"), list) else [],
        "prohibitions": metadata.get("prohibitions") if isinstance(metadata.get("prohibitions"), list) else [],
        "penalties": metadata.get("penalties") if isinstance(metadata.get("penalties"), list) else [],
        "definitions": metadata.get("definitions") if isinstance(metadata.get("definitions"), list) else [],
        "amendments": metadata.get("amendments") if isinstance(metadata.get("amendments"), list) else parsed.get("amendments", []),
        "change_events": metadata.get("change_events") if isinstance(metadata.get("change_events"), list) else parsed.get("change_events", []),
        "references": metadata.get("references") if isinstance(metadata.get("references"), list) else [],
        "confidence": metadata.get("confidence", 0.0),
        "provenance": {**parsed.get("provenance", {}), **(metadata.get("provenance") or {})},
    }


def _document_legal_identity(document: Document, suffix: str) -> str:
    return f"legal:{suffix}:{document.id}"


def _legal_fingerprint(value: str) -> str:
    return hashlib.sha256(canonical_entity_name(value).encode()).hexdigest()[:24]


def _remove_document_legal_projection(db: Session, document: Document) -> None:
    """Remove only generated evidence from one document; never touch manual rows."""
    relationship_ids = [row[0] for row in db.query(Relationship.id).join(RelationshipSource).filter(
        RelationshipSource.document_id == document.id, Relationship.origin == "legal_schema"
    ).all()]
    for relationship_id in relationship_ids:
        db.query(RelationshipSource).filter_by(relationship_id=relationship_id, document_id=document.id).delete(synchronize_session=False)
        relationship = db.get(Relationship, relationship_id)
        if relationship:
            relationship.source_count = db.query(func.count(RelationshipSource.id)).filter_by(relationship_id=relationship_id).scalar() or 0
            if relationship.source_count == 0:
                relationship.deleted_at = datetime.utcnow()
    entity_ids = [row[0] for row in db.query(Entity.id).join(EntitySource).filter(
        EntitySource.document_id == document.id, Entity.origin == "legal_schema"
    ).all()]
    for entity_id in entity_ids:
        db.query(EntitySource).filter_by(entity_id=entity_id, document_id=document.id).delete(synchronize_session=False)
        entity = db.get(Entity, entity_id)
        if entity:
            entity.source_count = db.query(func.count(EntitySource.id)).filter_by(entity_id=entity_id).scalar() or 0
            if entity.source_count == 0:
                entity.deleted_at = datetime.utcnow()
    db.flush()


def _upsert_legal_entity(db: Session, document: Document, *, name: str, entity_type: str, identity_key: str,
                         excerpt: str, attributes: dict | None = None, origin: str = "legal_schema",
                         review_status: str = "verified") -> tuple[Entity | None, bool]:
    name = str(name or "").strip()[:500]
    if not name:
        return None, False
    entity = db.query(Entity).filter_by(
        knowledge_base_id=document.knowledge_base_id, identity_key=identity_key, entity_type=entity_type,
    ).first()
    created = False
    if not entity:
        entity = Entity(
            knowledge_base_id=document.knowledge_base_id, name=name, canonical_name=canonical_entity_name(name),
            identity_key=identity_key, entity_type=entity_type, attributes=attributes or {}, confidence=1.0,
            origin=origin, review_status=review_status, is_legal=True,
        )
        db.add(entity); db.flush(); created = True
        db.add(GraphProjectionEvent(event_type="entity", entity_id=entity.id))
    else:
        entity.deleted_at, entity.name, entity.attributes = None, name, {**(entity.attributes or {}), **(attributes or {})}
        entity.origin, entity.review_status, entity.is_legal = origin, review_status, True
    excerpt = str(excerpt or name)[:5000]
    if not db.query(EntitySource).filter_by(entity_id=entity.id, document_id=document.id, excerpt=excerpt).first():
        db.add(EntitySource(entity_id=entity.id, document_id=document.id, excerpt=excerpt)); entity.source_count += 1
    return entity, created


def _upsert_legal_relationship(db: Session, document: Document, *, source: Entity | None, target: Entity | None,
                               relationship_type: str, excerpt: str, origin: str = "legal_schema",
                               review_status: str = "verified", confidence: float = 1.0, attributes: dict | None = None) -> tuple[Relationship | None, bool]:
    if not source or not target or source.id == target.id:
        return None, False
    relationship = db.query(Relationship).filter_by(
        knowledge_base_id=document.knowledge_base_id, source_entity_id=source.id,
        target_entity_id=target.id, relationship_type=relationship_type,
    ).first()
    created = False
    if not relationship:
        relationship = Relationship(
            knowledge_base_id=document.knowledge_base_id, source_entity_id=source.id, target_entity_id=target.id,
            relationship_type=relationship_type, confidence=confidence, attributes=attributes or {},
            origin=origin, review_status=review_status, is_legal=True,
        )
        db.add(relationship); db.flush(); created = True
        db.add(GraphProjectionEvent(event_type="relationship", relationship_id=relationship.id))
    else:
        # A reviewer decision is durable.  Rebuilding source metadata must not
        # silently turn an approved/rejected suggestion back into a pending one.
        if origin == "ai_suggestion" and relationship.origin == "ai_suggestion" and relationship.review_status in {"verified", "rejected"}:
            return relationship, False
        relationship.deleted_at, relationship.confidence = None, confidence
        relationship.attributes = {**(relationship.attributes or {}), **(attributes or {})}
        relationship.origin, relationship.review_status, relationship.is_legal = origin, review_status, True
    excerpt = str(excerpt or relationship_type)[:5000]
    if not db.query(RelationshipSource).filter_by(relationship_id=relationship.id, document_id=document.id, excerpt=excerpt).first():
        db.add(RelationshipSource(relationship_id=relationship.id, document_id=document.id, excerpt=excerpt)); relationship.source_count += 1
    return relationship, created


def legal_instrument_entity(db: Session, document: Document) -> Entity | None:
    return db.query(Entity).filter_by(
        knowledge_base_id=document.knowledge_base_id,
        identity_key=_document_legal_identity(document, "instrument"), entity_type="LegalInstrument",
    ).filter(Entity.deleted_at.is_(None)).first()


def sync_legal_document_graph(db: Session, document: Document, *, replace: bool = True) -> dict[str, int]:
    """Project evidence-backed Legal Graph v2 nodes and edges for one document."""
    if document.document_type not in LEGAL_DOCUMENT_TYPES:
        return {"entities": 0, "relationships": 0}
    if replace:
        _remove_document_legal_projection(db, document)
    if not document.legal_metadata:
        db.flush()
        return {"entities": 0, "relationships": 0}
    metadata = legal_metadata_v2(document)
    document.legal_metadata = metadata
    text = document.extracted_text or ""
    instrument = metadata.get("instrument") if isinstance(metadata.get("instrument"), dict) else {}
    title = _legal_value(instrument, "official_title", "title") or document.title or document.original_filename
    instrument_kind = _legal_value(instrument, "kind") or "Other"
    anchor, entity_created = _upsert_legal_entity(
        db, document, name=title, entity_type="LegalInstrument", identity_key=_document_legal_identity(document, "instrument"),
        excerpt=text[:500] or title, attributes={"instrument_kind": instrument_kind, "document_id": document.id,
        "official_number": instrument.get("official_number"), "effective_date": instrument.get("effective_date")},
    )
    entity_count, relationship_count = int(entity_created), 0

    issuer = instrument.get("issuer")
    issuer_name = _legal_value(issuer, "name", "organization")
    if issuer_name:
        issuer_entity, created = _upsert_legal_entity(
            db, document, name=issuer_name, entity_type="LegalAuthority",
            identity_key=f"legal:authority:{canonical_entity_name(issuer_name)}", excerpt=_legal_evidence(issuer, issuer_name),
        ); entity_count += int(created)
        _, created = _upsert_legal_relationship(db, document, source=anchor, target=issuer_entity, relationship_type="ISSUED_BY", excerpt=_legal_evidence(issuer, issuer_name)); relationship_count += int(created)

    for provision in metadata.get("provisions", []) if isinstance(metadata.get("provisions"), list) else []:
        number = _legal_value(provision, "number", "label")
        if not number:
            continue
        kind = _legal_value(provision, "kind") or "article"
        label = f"{kind.title()} {number}" if not str(number).startswith(("มาตรา", "ข้อ", "Article", "Clause")) else str(number)
        provision_entity, created = _upsert_legal_entity(
            db, document, name=label, entity_type="Provision",
            identity_key=f"legal:provision:{document.id}:{canonical_entity_name(kind)}:{canonical_entity_name(number)}",
            excerpt=_legal_evidence(provision, label), attributes={"provision_kind": kind, "provision_number": str(number), "heading": _legal_value(provision, "heading")},
        ); entity_count += int(created)
        _, created = _upsert_legal_relationship(db, document, source=anchor, target=provision_entity, relationship_type="CONTAINS_PROVISION", excerpt=_legal_evidence(provision, label)); relationship_count += int(created)

    for values_key, entity_type, relationship_type in [
        ("parties", "LegalParty", "PARTY_TO"), ("obligations", "Obligation", "REQUIRES"),
        ("rights", "Right", "GRANTS_RIGHT"), ("prohibitions", "Prohibition", "PROHIBITS"),
        ("definitions", "Definition", "DEFINES"), ("penalties", "Penalty", "REQUIRES"),
        ("amendments", "Amendment", "AMENDS"),
    ]:
        values = metadata.get(values_key) if isinstance(metadata.get(values_key), list) else []
        for item in values:
            name = _legal_value(item, "name", "party", "organization", "description", "obligation", "title", "term", "definition", "penalty")
            if not name:
                continue
            key = f"legal:{entity_type.casefold()}:{document.id}:{_legal_fingerprint(name)}"
            item_entity, created = _upsert_legal_entity(db, document, name=name, entity_type=entity_type, identity_key=key,
                excerpt=_legal_evidence(item, name), attributes={"role": _legal_value(item, "role")})
            entity_count += int(created)
            _, created = _upsert_legal_relationship(db, document, source=anchor, target=item_entity,
                relationship_type=relationship_type, excerpt=_legal_evidence(item, name))
            relationship_count += int(created)
    db.flush()
    return {"entities": entity_count, "relationships": relationship_count}


def _get_or_create_legal_family(db: Session, knowledge_base_id: str, title: str, normalized_key: str) -> LegalFamily:
    family = db.query(LegalFamily).filter_by(knowledge_base_id=knowledge_base_id, normalized_key=normalized_key).first()
    if family is None:
        family = LegalFamily(knowledge_base_id=knowledge_base_id, base_title=title[:500], normalized_key=normalized_key)
        db.add(family); db.flush()
    return family


def upsert_legal_instrument(db: Session, document: Document) -> LegalInstrument | None:
    """Register or refresh the legal registry entry for one legal document.

    Never touches `status`/`status_reason`; only resolve_instrument_statuses does,
    so extraction re-runs cannot clobber a status derived from reviewed relations.
    """
    if document.document_type not in LEGAL_DOCUMENT_TYPES or not document.legal_metadata:
        return None
    metadata = legal_metadata_v2(document)
    instrument_meta = metadata.get("instrument") if isinstance(metadata.get("instrument"), dict) else {}
    title = _legal_value(instrument_meta, "official_title", "title") or document.title or document.original_filename
    row = db.query(LegalInstrument).filter_by(document_id=document.id).first()
    if row is None:
        row = LegalInstrument(document_id=document.id, knowledge_base_id=document.knowledge_base_id,
                              status_source="resolver", review_status="unreviewed")
        db.add(row)
    if row.status_source != "manual":
        base_title_key, version_label, enacted_year = normalize_family_key(title)
        legal_work_key = _legal_value(instrument_meta, "legal_work_key") or base_title_key
        # Header parser canonicalizes the common Thai typo and amendment prefix.
        legal_work_key = canonical_entity_name(legal_work_key)
        family = _get_or_create_legal_family(db, document.knowledge_base_id, title, legal_work_key)
        row.family_id = family.id
        row.kind = classify_kind(title, _legal_value(instrument_meta, "kind") or None)
        row.authority_level = AUTHORITY_LEVELS.get(row.kind, AUTHORITY_LEVELS["other"])
        row.official_title = title[:500]
        row.official_number = _legal_value(instrument_meta, "official_number") or None
        row.issuer = _legal_value(instrument_meta.get("issuer"), "name", "organization") or None
        row.jurisdiction = _legal_value(instrument_meta, "jurisdiction") or None
        row.version_label = version_label
        row.enacted_year = enacted_year
        row.legal_work_key = legal_work_key
        row.document_class = _legal_value(instrument_meta, "document_class") or None
        row.version_date = parse_thai_date(instrument_meta.get("version_date")) or document.published_at
        row.effective_from = parse_thai_date(instrument_meta.get("effective_date")) or parse_thai_date(instrument_meta.get("version_date")) or document.published_at
        row.effective_to = parse_thai_date(instrument_meta.get("effective_to"))
        # Extraction may provide an official source, but a curator's manual
        # provenance must never be overwritten by a re-process.
        if not row.source_uri:
            row.source_uri = _legal_value(instrument_meta, "source_uri", "official_source_url", "source_url") or None
        if not row.source_reference:
            row.source_reference = _legal_value(instrument_meta, "source_reference", "gazette_reference", "ราชกิจจานุเบกษา") or None
    db.flush()
    return row


def _sync_deterministic_change_events(db: Session, knowledge_base_id: str) -> int:
    """Materialize explicit amendment/repeal clauses as verified registry edges.

    The target is the latest consolidated expression at or before the amendment
    date.  This prevents a 1977 amendment from being linked to the 2019 text.
    """
    instruments = db.query(LegalInstrument).filter_by(knowledge_base_id=knowledge_base_id).all()
    docs = {doc.id: doc for doc in db.query(Document).filter(Document.id.in_([row.document_id for row in instruments])).all()}
    by_id = {row.id: row for row in instruments}
    changed = 0
    for source in instruments:
        if source.document_class != "amendment":
            continue
        source_doc = docs.get(source.document_id)
        events = (legal_metadata_v2(source_doc).get("change_events", []) if source_doc else [])
        if not events:
            continue
        candidates = [row for row in instruments if row.id != source.id and row.legal_work_key == source.legal_work_key and row.document_class in {"main", "consolidated"}]
        source_date = source.version_date or source.effective_from
        prior = [row for row in candidates if not source_date or not row.version_date or row.version_date <= source_date]
        target = max(prior or candidates, key=lambda row: row.version_date or row.effective_from or datetime.min.date(), default=None)
        if not target:
            continue
        for event in events:
            action = str(event.get("action") or "replace").casefold()
            relation_type = "REPEALS" if action == "repeal" else "AMENDS"
            target_provision = str(event.get("target_provision") or event.get("provision_number") or "")[:120] or None
            row = db.query(LegalInstrumentRelation).filter_by(
                source_instrument_id=source.id, relation=relation_type,
                target_instrument_id=target.id, target_provision=target_provision,
            ).first()
            if not row:
                row = LegalInstrumentRelation(knowledge_base_id=knowledge_base_id,
                    source_instrument_id=source.id, target_instrument_id=target.id,
                    relation=relation_type, target_provision=target_provision,
                    origin="legal_schema", review_status="verified")
                db.add(row); changed += 1
            row.evidence_quote = str(event.get("evidence_quote") or "")[:5000]
            row.confidence, row.origin, row.review_status = 1.0, "legal_schema", "verified"
    db.flush()
    return changed


def _sync_legal_instrument_relation(db: Session, document: Document, target_document: Document | None,
                                    relationship: Relationship | None, relationship_type: str, reference: dict) -> None:
    """Mirror one cross-document suggestion into the legal registry so the status
    resolver and query-time resolver can reason about it without re-parsing text."""
    if relationship_type not in LEGAL_INSTRUMENT_RELATION_TYPES:
        return
    source_instrument = db.query(LegalInstrument).filter_by(document_id=document.id).first()
    if not source_instrument:
        return
    target_instrument = db.query(LegalInstrument).filter_by(document_id=target_document.id).first() if target_document else None
    target_provision = _legal_value(reference, "target_provision") or None
    row = db.query(LegalInstrumentRelation).filter_by(
        source_instrument_id=source_instrument.id, relation=relationship_type,
        target_instrument_id=target_instrument.id if target_instrument else None,
        target_provision=target_provision,
    ).first()
    if row and row.origin == "manual":
        return
    if not row:
        row = LegalInstrumentRelation(
            knowledge_base_id=document.knowledge_base_id, source_instrument_id=source_instrument.id,
            relation=relationship_type, origin="ai_suggestion", review_status="suggested",
        )
        db.add(row)
    row.target_instrument_id = target_instrument.id if target_instrument else None
    row.target_text = (_legal_value(reference, "target_title", "title", "instrument_title") or None or "")[:700] or None
    row.target_provision = target_provision
    row.evidence_quote = _legal_evidence(reference, relationship_type)[:5000]
    row.confidence = _reference_confidence(reference)
    row.relationship_id = relationship.id if relationship else None
    if relationship and relationship.review_status in {"verified", "rejected"}:
        row.review_status = relationship.review_status
    db.flush()


def sync_legal_instrument_relation_review(db: Session, relationship: Relationship) -> None:
    """Propagate an admin's approve/reject decision to the registry and re-resolve status."""
    rows = db.query(LegalInstrumentRelation).filter_by(relationship_id=relationship.id).all()
    manual_rows = [row for row in rows if row.origin != "manual"]
    for row in manual_rows:
        row.review_status = relationship.review_status
    db.flush()
    for knowledge_base_id in {row.knowledge_base_id for row in manual_rows}:
        resolve_instrument_statuses(db, knowledge_base_id)


def _clear_legal_suggestions(db: Session, knowledge_base_id: str) -> None:
    rows = db.query(Relationship).filter_by(knowledge_base_id=knowledge_base_id, origin="ai_suggestion", is_legal=True, review_status="suggested").all()
    for relationship in rows:
        relationship.deleted_at = datetime.utcnow()


def _target_instrument(db: Session, document: Document, reference: dict) -> Document | None:
    title = _legal_value(reference, "target_title", "title", "instrument_title")
    number = _legal_value(reference, "target_number", "official_number", "instrument_number")
    candidates = db.query(Document).filter(
        Document.knowledge_base_id == document.knowledge_base_id, Document.id != document.id,
        Document.document_type.in_(LEGAL_DOCUMENT_TYPES), Document.status == "completed", Document.deleted_at.is_(None),
    ).all()
    matches = []
    source_meta = legal_metadata_v2(document).get("instrument") or {}
    source_work = canonical_entity_name(_legal_value(source_meta, "legal_work_key"))
    source_date = parse_thai_date(source_meta.get("version_date")) or document.published_at
    for candidate in candidates:
        metadata = legal_metadata_v2(candidate)
        instrument = metadata.get("instrument") or {}
        candidate_title = _legal_value(instrument, "official_title") or candidate.title or candidate.original_filename
        candidate_work = canonical_entity_name(_legal_value(instrument, "legal_work_key"))
        if source_work and candidate_work and source_work != candidate_work:
            continue
        score = 0
        if number and number == str(instrument.get("official_number") or ""):
            score += 100
        if title and (canonical_entity_name(title) in canonical_entity_name(candidate_title) or canonical_entity_name(candidate_title) in canonical_entity_name(title)):
            score += 50
        if score:
            candidate_date = parse_thai_date(instrument.get("version_date")) or candidate.published_at
            # Prefer the latest expression not newer than the referring act.
            if source_date and candidate_date and candidate_date > source_date:
                score -= 20
            matches.append((score, candidate_date or datetime.min.date(), candidate))
    return max(matches, key=lambda item: (item[0], item[1]))[2] if matches else None


def build_legal_cross_document_suggestions(db: Session, knowledge_base_id: str) -> int:
    """Create reviewable, evidenced cross-instrument links from extracted references."""
    count = 0
    documents = db.query(Document).filter(
        Document.knowledge_base_id == knowledge_base_id, Document.document_type.in_(LEGAL_DOCUMENT_TYPES),
        Document.status == "completed", Document.deleted_at.is_(None),
    ).all()
    for document in documents:
        source = legal_instrument_entity(db, document)
        metadata = legal_metadata_v2(document)
        references = metadata.get("references", []) if isinstance(metadata.get("references"), list) else []
        if not references and document.extracted_text:
            candidates = []
            for candidate in documents:
                if candidate.id == document.id:
                    continue
                candidate_metadata = legal_metadata_v2(candidate)
                candidate_instrument = candidate_metadata.get("instrument") or {}
                candidates.append({
                    "title": _legal_value(candidate_instrument, "official_title") or candidate.title or candidate.original_filename,
                    "official_number": str(candidate_instrument.get("official_number") or ""),
                })
            references = OpenRouterClient().suggest_legal_relationships(document.title or document.original_filename, document.extracted_text, candidates)
            if references:
                metadata["references"] = references
                document.legal_metadata = metadata
        for reference in references:
            if not isinstance(reference, dict):
                continue
            relationship_type = _legal_value(reference, "relationship", "relationship_type").upper() or "REFERS_TO"
            if relationship_type not in LEGAL_GRAPH_RELATIONSHIPS:
                relationship_type = "REFERS_TO"
            target_document = _target_instrument(db, document, reference)
            target = legal_instrument_entity(db, target_document) if target_document else None
            relationship, created = _upsert_legal_relationship(
                db, document, source=source, target=target, relationship_type=relationship_type,
                excerpt=_legal_evidence(reference, relationship_type), origin="ai_suggestion", review_status="suggested",
                confidence=_reference_confidence(reference), attributes={"reference": reference},
            )
            count += int(created)
            _sync_legal_instrument_relation(db, document, target_document, relationship, relationship_type, reference)
    db.flush()
    return count


def rebuild_legal_graph(db: Session, knowledge_base_id: str) -> dict[str, int]:
    documents = db.query(Document).filter(
        Document.knowledge_base_id == knowledge_base_id, Document.document_type.in_(LEGAL_DOCUMENT_TYPES),
        Document.status == "completed", Document.deleted_at.is_(None),
    ).all()
    totals = {"documents": len(documents), "entities": 0, "relationships": 0, "suggestions": 0}
    _clear_legal_suggestions(db, knowledge_base_id)
    for document in documents:
        result = sync_legal_document_graph(db, document, replace=True)
        totals["entities"] += result["entities"]; totals["relationships"] += result["relationships"]
        upsert_legal_instrument(db, document)
        link_provisions_to_chunks(db, document)
    totals["deterministic_relations"] = _sync_deterministic_change_events(db, knowledge_base_id)
    totals["suggestions"] = build_legal_cross_document_suggestions(db, knowledge_base_id)
    totals["status_changes"] = resolve_instrument_statuses(db, knowledge_base_id)["changed"]
    db.commit()
    return totals


def resolve_entity(db: Session, knowledge_base_ids: list[str], text: str) -> Entity | None:
    canonical = canonical_entity_name(text)
    query = db.query(Entity).filter(Entity.deleted_at.is_(None))
    if knowledge_base_ids:
        query = query.filter(Entity.knowledge_base_id.in_(knowledge_base_ids))
    exact = query.filter(Entity.canonical_name == canonical).first()
    if exact:
        return exact
    candidates = query.filter(Entity.name.ilike(f"%{text}%")).all()
    if candidates:
        return candidates[0]
    # JSON aliases are portable between PostgreSQL and SQLite when matched in
    # Python; the graph is bounded and this runs only after indexed lookups.
    for candidate in query.limit(500).all():
        if canonical in {canonical_entity_name(alias) for alias in (candidate.aliases or [])}:
            return candidate
    return None


def relationship_sources(db: Session, relationships: list[Relationship], plan: RetrievalPlan | None = None) -> list[dict]:
    ids = [relationship.id for relationship in relationships]
    if not ids:
        return []
    rows = db.query(RelationshipSource, Document).join(Document, Document.id == RelationshipSource.document_id).filter(
        RelationshipSource.relationship_id.in_(ids), Document.status == "completed", Document.deleted_at.is_(None)
    )
    if plan and plan.published_from:
        rows = rows.filter(Document.published_at >= plan.published_from)
    if plan and plan.published_to:
        rows = rows.filter(Document.published_at < plan.published_to)
    rows = rows.all()
    return [{"citation_id": f"S{index}", "document_id": document.id, "title": document.title,
             "chunk_id": source.id, "excerpt": source.excerpt, "relevance": 1.0 / index,
             "_relationship_id": source.relationship_id}
            for index, (source, document) in enumerate(rows, 1)]


def entity_graph(db: Session, entity: Entity, depth: int = 1) -> dict:
    visited = {entity.id}
    frontier = {entity.id}
    edges: list[Relationship] = []
    for _ in range(depth):
        found = db.query(Relationship).filter(Relationship.deleted_at.is_(None)).filter(
            (Relationship.is_legal.is_(False)) | (Relationship.review_status == "verified")
        ).filter(Relationship.knowledge_base_id == entity.knowledge_base_id).filter(
            (Relationship.source_entity_id.in_(frontier)) | (Relationship.target_entity_id.in_(frontier))).all()
        edges.extend(edge for edge in found if edge.id not in {item.id for item in edges})
        next_frontier = {edge.source_entity_id for edge in found} | {edge.target_entity_id for edge in found}
        next_frontier -= visited
        visited |= next_frontier
        frontier = next_frontier
        if not frontier:
            break
    nodes = db.query(Entity).filter(Entity.id.in_(visited)).all()
    return {
        "nodes": [{"id": node.id, "name": node.name, "type": node.entity_type} for node in nodes],
        "edges": [{"id": edge.id, "source": edge.source_entity_id, "target": edge.target_entity_id, "type": edge.relationship_type} for edge in edges],
    }


def analyze_impact(db: Session, subject: str, knowledge_base_ids: list[str], max_depth: int, include_indirect: bool, entity_id: str | None = None) -> dict:
    entity = db.get(Entity, entity_id) if entity_id else resolve_entity(db, knowledge_base_ids, subject)
    if entity and knowledge_base_ids and entity.knowledge_base_id not in knowledge_base_ids:
        entity = None
    if not entity:
        return {"status": "success", "subject": None, "direct_impacts": [], "indirect_impacts": [], "sources": [], "insufficient_evidence": True, "warnings": ["Entity was not found."]}
    graph = entity_graph(db, entity, max_depth if include_indirect else 1)
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges_by_node: dict[str, list[dict]] = {}
    for edge in graph["edges"]:
        edges_by_node.setdefault(edge["source"], []).append(edge)
        edges_by_node.setdefault(edge["target"], []).append(edge)
    distance = {entity.id: 0}
    paths = {entity.id: [entity.id]}
    frontier = [entity.id]
    selected_edges: list[Relationship] = []
    while frontier:
        current = frontier.pop(0)
        if distance[current] >= max_depth:
            continue
        for edge in edges_by_node.get(current, []):
            other = edge["target"] if edge["source"] == current else edge["source"]
            if other in distance:
                continue
            distance[other] = distance[current] + 1
            paths[other] = paths[current] + [other]
            frontier.append(other)
            selected_edges.append(db.get(Relationship, edge["id"]))
    sources = relationship_sources(db, [edge for edge in selected_edges if edge])
    citation_ids_by_rel: dict[str, list[str]] = {}
    for source in sources:
        citation_ids_by_rel.setdefault(source.pop("_relationship_id"), []).append(source["citation_id"])
    direct, indirect = [], []
    for node_id, node in nodes.items():
        if node_id == entity.id or node_id not in distance:
            continue
        edge = next((item for item in selected_edges if item and (item.source_entity_id == node_id or item.target_entity_id == node_id)), None)
        item = {"entity_id": node_id, "name": node["name"], "type": node["type"], "distance": distance[node_id],
                "path": [nodes[path_id]["name"] for path_id in paths[node_id]], "relationship": edge.relationship_type if edge else "RELATED_TO",
                "confidence": edge.confidence if edge else None, "citation_ids": citation_ids_by_rel.get(edge.id, []) if edge else []}
        (direct if distance[node_id] == 1 else indirect).append(item)
    return {"status": "success", "subject": {"entity_id": entity.id, "name": entity.name, "type": entity.entity_type},
            "direct_impacts": direct, "indirect_impacts": indirect if include_indirect else [], "sources": sources,
            "insufficient_evidence": not bool(direct or indirect), "warnings": []}


def store_upload(upload, knowledge_base_id: str) -> tuple[str, str, int, str, str]:
    settings = get_settings()
    extension = Path(upload.filename or "").suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError("FILE_TYPE_NOT_SUPPORTED")
    reported_mime = (upload.content_type or "").lower()
    if reported_mime and reported_mime not in {"application/octet-stream", "binary/octet-stream"} and reported_mime not in ALLOWED_MIME_TYPES[extension]:
        raise ValueError("FILE_MIME_TYPE_NOT_SUPPORTED")
    root = settings.file_root / knowledge_base_id
    root.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4()}{extension}"
    destination = root / stored_name
    size = 0
    digest = hashlib.sha256()
    with destination.open("wb") as output:
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_file_size_mb * 1024 * 1024:
                destination.unlink(missing_ok=True)
                raise ValueError("FILE_TOO_LARGE")
            digest.update(chunk)
            output.write(chunk)
    mime_type = upload.content_type or mimetypes.guess_type(upload.filename or "")[0] or "application/octet-stream"
    return str(destination), stored_name, size, digest.hexdigest(), mime_type


DOCUMENT_TYPES = {"general", "legal", "regulation", "contract"}
LEGAL_DOCUMENT_TYPES = {"legal", "regulation", "contract"}


def create_document_job(db: Session, knowledge_base_id: str, upload, title: str | None = None, document_type: str = "general", published_at=None) -> tuple[Document, ProcessingJob]:
    if document_type not in DOCUMENT_TYPES:
        raise ValueError("DOCUMENT_TYPE_INVALID")
    path, stored, size, checksum, mime = store_upload(upload, knowledge_base_id)
    duplicate = db.query(Document).filter_by(knowledge_base_id=knowledge_base_id, checksum_sha256=checksum).filter(Document.deleted_at.is_(None)).first()
    if duplicate:
        Path(path).unlink(missing_ok=True)
        raise ValueError("FILE_DUPLICATE")
    doc = Document(knowledge_base_id=knowledge_base_id, original_filename=upload.filename or stored,
                   stored_filename=stored, storage_path=path, file_size=size, checksum_sha256=checksum,
                   mime_type=mime, title=title or Path(upload.filename or stored).stem,
                   document_type=document_type, published_at=published_at, status="queued")
    db.add(doc); db.flush()
    job = ProcessingJob(document_id=doc.id, knowledge_base_id=knowledge_base_id)
    db.add(job); db.commit(); db.refresh(doc); db.refresh(job)
    return doc, job


def _legacy_extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".txt", ".md", ".csv", ".json"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if ext in {".html", ".htm"}:
        return BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser").get_text(" ", strip=True)
    if ext == ".docx":
        return "\n".join(p.text for p in WordDocument(path).paragraphs)
    if ext == ".pdf":
        reader = PdfReader(path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if len(text.strip()) < 20:
            raise RuntimeError("OCR_REQUIRED")
        return text
    raise RuntimeError("FILE_TYPE_NOT_SUPPORTED")


def _markitdown_extract(path: Path) -> str:
    """Convert a validated local upload to Markdown without plugins, OCR, LLMs, or cloud services."""
    result = MarkItDown(enable_builtins=True, enable_plugins=False).convert_local(path)
    return result.text_content.strip()


def _has_meaningful_text(text: str) -> bool:
    return len(re.sub(r"[\W_]+", "", text, flags=re.UNICODE)) >= 20


def extract_text(document: Document) -> str:
    path = Path(document.storage_path)
    ext = path.suffix.lower()
    try:
        text = _markitdown_extract(path)
        if ext == ".pdf" and not _has_meaningful_text(text):
            raise RuntimeError("OCR_REQUIRED")
        if text:
            return text
        raise RuntimeError("TEXT_EXTRACTION_EMPTY")
    except RuntimeError as exc:
        if str(exc) == "OCR_REQUIRED":
            raise
        primary_error = exc
    except Exception as exc:
        primary_error = exc
    if ext in LEGACY_EXTRACTOR_EXTENSIONS:
        try:
            text = _legacy_extract_text(path)
            if ext == ".pdf" and not _has_meaningful_text(text):
                raise RuntimeError("OCR_REQUIRED")
            if text.strip():
                return text
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("TEXT_EXTRACTION_FAILED") from exc
    raise RuntimeError("TEXT_EXTRACTION_FAILED") from primary_error


def extract_scanned_pdf_with_external_ocr(document: Document, job: ProcessingJob, db: Session) -> str:
    """Use the remote OCR service only after local PDF text-layer extraction fails."""
    settings = get_settings()
    client = ExternalOcrClient(settings)

    def progress(stage: str, percent: int) -> None:
        job.current_stage, job.progress_percent = stage, percent
        db.commit()

    return client.extract_markdown(Path(document.storage_path), progress)


def split_text(text: str, chunk_size: int, overlap: int) -> list[tuple[int, int, str]]:
    """Split normalized text on word boundaries with bounded overlap."""
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + chunk_size)
        if end < len(normalized):
            boundary = normalized.rfind(" ", start + max(1, chunk_size // 2), end)
            if boundary > start:
                end = boundary
        content = normalized[start:end].strip()
        if content:
            chunks.append((start, end, content))
        if end >= len(normalized):
            break
        start = max(end - overlap, start + 1)
    return chunks


_LEGAL_SECTION_PATTERN = re.compile(
    r"^[ \t]*(มาตรา|ข้อ|หมวด|ส่วนที่|บทเฉพาะกาล|บทนิยาม)\s*([0-9๐-๙]+(?:/[0-9๐-๙]+)?)?\s*(ทวิ|ตรี|จัตวา|เบญจ)?",
    re.MULTILINE,
)
_LEGAL_SECTION_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def _legal_section_identity(raw_kind: str, number: str | None, suffix: str | None) -> tuple[str, str | None, str]:
    if not number:
        return raw_kind, None, raw_kind
    normalized_number = number.translate(_LEGAL_SECTION_DIGITS) + (f" {suffix}" if suffix else "")
    return raw_kind, normalized_number, f"{raw_kind} {normalized_number}"


def split_legal_text(text: str, chunk_size: int, overlap: int) -> list[tuple[int, int, str, str | None, str | None, str | None]]:
    """Split on มาตรา/ข้อ/หมวด headings so each chunk keeps its section identity.

    A heading is only recognized at the start of a line, so a mid-sentence
    cross-reference like "ให้เป็นไปตามมาตรา 15" never starts a new section. A
    section body that still exceeds chunk_size falls back to split_text's
    word-boundary sub-split, and every resulting piece inherits that section's
    identity. Text before the first heading is tagged "preamble".
    """
    text = text or ""
    matches = list(_LEGAL_SECTION_PATTERN.finditer(text))
    spans: list[tuple[int, int, str, str | None, str | None]] = []
    if not matches:
        spans.append((0, len(text), "preamble", None, None))
    else:
        if matches[0].start() > 0:
            spans.append((0, matches[0].start(), "preamble", None, None))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            kind, number, label = _legal_section_identity(match.group(1), match.group(2), match.group(3))
            spans.append((match.start(), end, kind, number, label))
    pieces: list[tuple[int, int, str, str | None, str | None, str | None]] = []
    for span_start, span_end, section_kind, section_number, section_label in spans:
        for offset, sub_end, content in split_text(text[span_start:span_end], chunk_size, overlap):
            pieces.append((span_start + offset, span_start + sub_end, content, section_kind, section_number, section_label))
    return pieces


def replace_document_chunks(db: Session, document: Document, text: str) -> None:
    settings = get_settings()
    db.query(DocumentChunk).filter_by(document_id=document.id).delete(synchronize_session=False)
    splitter = split_legal_text if document.document_type in LEGAL_DOCUMENT_TYPES else split_text
    for index, piece in enumerate(splitter(text, settings.default_chunk_size, settings.default_chunk_overlap)):
        char_start, char_end, content = piece[0], piece[1], piece[2]
        section_kind, section_number, section_label = piece[3:] if len(piece) > 3 else (None, None, None)
        db.add(DocumentChunk(
            document_id=document.id,
            knowledge_base_id=document.knowledge_base_id,
            chunk_index=index,
            content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            char_start=char_start,
            char_end=char_end,
            token_count=len(re.findall(r"\S+", content)),
            section_kind=section_kind,
            section_number=section_number,
            section_label=section_label,
        ))
    db.flush()


def link_provisions_to_chunks(db: Session, document: Document) -> int:
    """Attach matching chunk IDs to each Provision entity so the query-time
    resolver can fetch a provision's exact chunk instead of relying on similarity."""
    if document.document_type not in LEGAL_DOCUMENT_TYPES:
        return 0
    provisions = db.query(Entity).filter(
        Entity.knowledge_base_id == document.knowledge_base_id, Entity.entity_type == "Provision",
        Entity.identity_key.like(f"legal:provision:{document.id}:%"), Entity.deleted_at.is_(None),
    ).all()
    if not provisions:
        return 0
    chunks = db.query(DocumentChunk).filter(
        DocumentChunk.document_id == document.id, DocumentChunk.section_number.is_not(None),
    ).all()
    by_number: dict[str, list[str]] = {}
    for chunk in chunks:
        by_number.setdefault(re.sub(r"\s+", "", chunk.section_number).casefold(), []).append(chunk.id)
    linked = 0
    for provision in provisions:
        number = (provision.attributes or {}).get("provision_number")
        chunk_ids = by_number.get(re.sub(r"\s+", "", str(number)).casefold()) if number else None
        if not chunk_ids:
            continue
        provision.attributes = {**(provision.attributes or {}), "chunk_ids": chunk_ids}
        linked += 1
    db.flush()
    return linked


def embed_document_chunks(db: Session, document_id: str) -> None:
    chunks = db.query(DocumentChunk).filter_by(document_id=document_id).order_by(DocumentChunk.chunk_index).all()
    if not chunks:
        return
    client = OpenRouterClient()
    if not client.embeddings_enabled:
        return
    vectors = client.embed_texts([chunk.content for chunk in chunks])
    for chunk, vector in zip(chunks, vectors, strict=True):
        chunk.embedding = vector
    db.flush()


def queue_embedding_reindex(db: Session, knowledge_base_id: str, force: bool = False) -> int:
    documents = db.query(Document).filter_by(knowledge_base_id=knowledge_base_id, status="completed").filter(Document.deleted_at.is_(None)).all()
    queued = 0
    for document in documents:
        # SQLite represents a JSON null as a non-SQL-NULL value in tests, so
        # inspect the mapped value rather than relying on `IS NOT NULL` alone.
        has_embedding = any(chunk.embedding for chunk in db.query(DocumentChunk).filter_by(document_id=document.id))
        active = db.query(ProcessingJob.id).filter(
            ProcessingJob.document_id == document.id,
            ProcessingJob.job_type == "REINDEX_EMBEDDINGS",
            ProcessingJob.status.in_(["queued", "running"]),
        ).first()
        if (force or not has_embedding) and not active:
            db.add(ProcessingJob(document_id=document.id, knowledge_base_id=knowledge_base_id, job_type="REINDEX_EMBEDDINGS"))
            queued += 1
    db.commit()
    return queued


def process_next_job(db: Session) -> bool:
    job = db.query(ProcessingJob).filter(
        ProcessingJob.status == "queued", ProcessingJob.next_attempt_at <= datetime.utcnow()
    ).order_by(ProcessingJob.created_at).first()
    if not job:
        return False
    if job.job_type == "REBUILD_LEGAL_GRAPH":
        job.status, job.current_stage, job.progress_percent, job.attempt_count = "running", "rebuilding_legal_graph", 10, job.attempt_count + 1
        db.commit()
        try:
            if not job.knowledge_base_id:
                raise RuntimeError("KNOWLEDGE_BASE_NOT_FOUND")
            rebuild_legal_graph(db, job.knowledge_base_id)
            job.status, job.current_stage, job.progress_percent = "completed", "completed", 100
        except Exception as exc:
            logger.exception("legal graph rebuild failed", extra={"job_id": job.id, "knowledge_base_id": job.knowledge_base_id})
            job.status, job.current_stage, job.error_code, job.error_message = "failed", "failed", "LEGAL_GRAPH_REBUILD_FAILED", str(exc)[:2000]
        db.commit()
        return True
    doc = db.get(Document, job.document_id)
    if not doc or doc.deleted_at:
        job.status, job.current_stage, job.error_code = "cancelled", "cancelled", "DOCUMENT_DELETED"
        db.commit()
        return True
    reindex_only = job.job_type == "REINDEX_EMBEDDINGS"
    legal_only = job.job_type == "EXTRACT_LEGAL_METADATA"
    job.status, job.current_stage, job.progress_percent, job.attempt_count = "running", "extracting", 10, job.attempt_count + 1
    # Legal metadata runs as a follow-up job.  It must never move an already
    # searchable document back to "extracting" or re-read the source file.
    if not reindex_only and not legal_only:
        doc.status = "extracting"
    db.commit()
    try:
        if (reindex_only or legal_only) and doc.extracted_text:
            text = doc.extracted_text
        else:
            try:
                text = extract_text(doc)
            except RuntimeError as exc:
                if str(exc) != "OCR_REQUIRED" or not get_settings().ext_ocr_key:
                    raise
                job.current_stage, job.progress_percent = "external_ocr_submit", 12
                db.commit()
                text = extract_scanned_pdf_with_external_ocr(doc, job, db)
        if not reindex_only:
            doc.extracted_text = text
        if legal_only:
            job.current_stage, job.progress_percent = "legal_extraction", 60
            db.commit()
            deterministic = parse_legal_corpus_metadata(text, doc.title or doc.original_filename)
            try:
                extracted = OpenRouterClient().extract_legal_metadata(doc.title or doc.original_filename, text)
            except Exception:
                # Header/change clauses are sufficient to preserve a traceable
                # legal instrument even when the optional LLM is unavailable.
                extracted = {}
            if isinstance(extracted, dict):
                extracted["schema_version"] = 2
                extracted["instrument"] = {**(extracted.get("instrument") or {}), **deterministic["instrument"]}
                extracted["change_events"] = deterministic.get("change_events", []) or extracted.get("change_events", [])
                extracted["amendments"] = deterministic.get("amendments", []) or extracted.get("amendments", [])
                extracted["provenance"] = {**(extracted.get("provenance") or {}), **deterministic.get("provenance", {})}
                doc.legal_metadata = extracted
            else:
                doc.legal_metadata = deterministic
            sync_legal_document_graph(db, doc)
            upsert_legal_instrument(db, doc)
            link_provisions_to_chunks(db, doc)
            resolve_instrument_statuses(db, doc.knowledge_base_id)
            job.status, job.current_stage, job.progress_percent = "completed", "completed", 100
            db.commit()
            return True
        job.current_stage, job.progress_percent = "chunking", 45
        existing_chunks = db.query(DocumentChunk.id, DocumentChunk.section_kind).filter_by(document_id=doc.id).all() if reindex_only else []
        # A forced reindex re-chunks a legal document that predates section-aware
        # splitting (no chunk has a section identity yet); every other reindex
        # keeps its existing chunks and only refreshes embeddings.
        needs_legal_rechunk = doc.document_type in LEGAL_DOCUMENT_TYPES and existing_chunks and not any(section_kind for _, section_kind in existing_chunks)
        if reindex_only and existing_chunks and not needs_legal_rechunk:
            pass
        else:
            replace_document_chunks(db, doc, text)
        db.commit()
        job.current_stage, job.progress_percent = "embedding", 60
        embed_document_chunks(db, doc.id)
        db.commit()
        engine = LightRAGRetrievalEngine()
        if engine.enabled and not reindex_only:
            job.current_stage, job.progress_percent = "indexing", 70
            db.commit()
            doc.external_engine_id = engine.ingest(doc.id, doc.knowledge_base_id, text, doc.title or doc.original_filename)
            if doc.external_engine_id:
                deadline = time.monotonic() + get_settings().lightrag_processing_timeout_seconds
                while time.monotonic() < deadline:
                    status = engine.track_status(doc.external_engine_id)
                    if status == "processed":
                        break
                    if status == "failed":
                        raise RuntimeError("RETRIEVAL_ENGINE_REJECTED")
                    job.progress_percent = 85
                    db.commit()
                    time.sleep(2)
                else:
                    raise RuntimeError("RETRIEVAL_ENGINE_TIMEOUT")
            sync_lightrag_document_graph(db, doc)
        if not reindex_only and not legal_only:
            doc.status = "completed"; doc.indexed_at = datetime.utcnow()
            if doc.document_type in LEGAL_DOCUMENT_TYPES:
                db.add(ProcessingJob(
                    document_id=doc.id,
                    knowledge_base_id=doc.knowledge_base_id,
                    job_type="EXTRACT_LEGAL_METADATA",
                    current_stage="queued",
                ))
        job.status, job.current_stage, job.progress_percent = "completed", "completed", 100
    except Exception as exc:
        code = str(exc) if str(exc) in {"OCR_REQUIRED", "FILE_TYPE_NOT_SUPPORTED", "RETRIEVAL_ENGINE_UNAVAILABLE", "RETRIEVAL_ENGINE_REJECTED", "RETRIEVAL_ENGINE_BUSY", "RETRIEVAL_ENGINE_TIMEOUT", "OPENROUTER_UNAVAILABLE", "OPENROUTER_EMBEDDING_INVALID_RESPONSE", "OPENROUTER_EMBEDDING_DIMENSION_MISMATCH", "OPENROUTER_LLM_INVALID_RESPONSE", "EXTERNAL_OCR_NOT_CONFIGURED", "EXTERNAL_OCR_UNAVAILABLE", "EXTERNAL_OCR_REJECTED", "EXTERNAL_OCR_TIMEOUT", "EXTERNAL_OCR_EMPTY_RESULT", "EXTERNAL_OCR_INVALID_RESPONSE"} else "TEXT_EXTRACTION_FAILED"
        logger.exception("document processing failed", extra={"document_id": doc.id, "job_id": job.id, "error_code": code})
        message = "The document could not be processed."
        job.error_code, job.error_message = code, message
        if code in TRANSIENT_PROCESSING_ERRORS and job.attempt_count < MAX_PROCESSING_ATTEMPTS:
            job.status, job.current_stage = "queued", "retry_wait"
            job.next_attempt_at = datetime.utcnow() + timedelta(seconds=processing_retry_delay(job.attempt_count))
            doc.status, doc.error_code, doc.error_message = "queued", code, "Temporary dependency failure; retry scheduled."
        else:
            if legal_only:
                job.error_message = "Legal metadata extraction failed; the source document remains available."
            elif code == "OCR_REQUIRED":
                doc.status, doc.error_code, doc.error_message = "ocr_required", code, "This PDF has no text layer. OCR is required before it can be searched."
            else:
                doc.status, doc.error_code, doc.error_message = "failed", code, message
            job.status = "failed"
    db.commit()
    return True


def process_next_graph_projection(db: Session) -> bool:
    """Deliver one transactionally-recorded graph event with bounded retry backoff."""
    store = Neo4jGraphStore()
    if not store.enabled:
        return False
    event = db.query(GraphProjectionEvent).filter(
        GraphProjectionEvent.status == "queued", GraphProjectionEvent.next_attempt_at <= datetime.utcnow()
    ).order_by(GraphProjectionEvent.created_at).first()
    if not event:
        return False
    event.status, event.attempt_count = "running", event.attempt_count + 1
    db.commit()
    try:
        if event.event_type == "entity" and event.entity_id:
            entity = db.get(Entity, event.entity_id)
            if entity:
                store.upsert_entity(entity)
        elif event.event_type == "relationship" and event.relationship_id:
            relationship = db.get(Relationship, event.relationship_id)
            if relationship:
                source, target = db.get(Entity, relationship.source_entity_id), db.get(Entity, relationship.target_entity_id)
                if source and target:
                    store.upsert_entity(source)
                    store.upsert_entity(target)
                    store.upsert_relationship(relationship, source, target)
        event.status, event.completed_at, event.last_error = "completed", datetime.utcnow(), None
    except (httpx.HTTPError, RuntimeError) as exc:
        event.status = "queued"
        event.last_error = str(exc)[:2000]
        event.next_attempt_at = datetime.utcnow() + timedelta(seconds=min(60, 2 ** event.attempt_count))
    db.commit()
    return True


def plan_intent(query: str) -> str:
    value = query.lower()
    if any(word in value for word in ["impact", "affected", "ผลกระทบ", "หยุดทำงาน"]): return "impact_analysis"
    if any(word in value for word in ["depend", "relationship", "เชื่อม", "สัมพันธ์"]): return "relationship_lookup"
    if re.search(r"\b[a-z]+[-_]?[0-9]{2,}\b", value): return "entity_lookup"
    return "hybrid_fallback"


def build_retrieval_plan(db: Session, query: str, kb_ids: list[str], max_sources: int, query_filters=None,
                         trace: list[dict] | None = None) -> PlannerDecision:
    """Build a policy-constrained plan; invoke the LLM only when rules are ambiguous."""
    rows = db.query(KnowledgeBase).filter(KnowledgeBase.id.in_(kb_ids)).all() if kb_ids else []
    policy = intersect_policies([policy_from_config(row.retrieval_config) for row in rows])
    decision = rule_plan(query, policy, max_sources)
    requested_from = getattr(query_filters, "published_from", None) if query_filters else None
    requested_to = getattr(query_filters, "published_to", None) if query_filters else None
    as_of_date = getattr(query_filters, "as_of_date", None) if query_filters else None
    include_historical = bool(getattr(query_filters, "include_historical", False)) if query_filters else False
    decision.plan = decision.plan.model_copy(update={
        "published_from": requested_from or decision.plan.published_from,
        "published_to": requested_to or decision.plan.published_to,
        "as_of_date": as_of_date or decision.plan.as_of_date,
        "include_historical": include_historical or decision.plan.include_historical,
    })
    if decision.ambiguous and policy.planner_llm_fallback:
        try:
            available = [channel.value for channel, enabled in (
                (RetrievalChannel.VECTOR, policy.enable_vector),
                (RetrievalChannel.FULLTEXT, policy.enable_fulltext),
                (RetrievalChannel.GRAPH, policy.enable_graph),
                (RetrievalChannel.LIGHTRAG, policy.enable_lightrag),
            ) if enabled]
            value = OpenRouterClient().plan_retrieval(query, available, decision.plan.max_sources, policy.maximum_graph_depth)
            decision = apply_llm_plan(decision, value, policy, max_sources)
        except (RuntimeError, ValueError, TypeError) as exc:
            decision.plan = decision.plan.model_copy(update={"planner_source": "rules_fallback", "fallback_reason": str(exc)})
    started_at = time.monotonic()
    legal_context = resolve_legal_context(db, query, kb_ids, decision.plan, policy)
    if legal_context is not None:
        decision.plan = decision.plan.model_copy(update={"legal_context": legal_context})
        _append_retrieval_trace(trace, channel="legal_resolver", system="PostgreSQL legal registry", status="used",
                                started_at=started_at, result_count=len(legal_context.current_version_ids),
                                detail=f"matched={len(legal_context.matched_instrument_ids)} excluded={len(legal_context.excluded_document_ids)}")
    metrics.observe_planner(decision.plan.intent, decision.plan.planner_source, decision.plan.fallback_reason is not None)
    return decision


def _append_retrieval_trace(trace: list[dict] | None, *, channel: str, system: str, status: str,
                            started_at: float, result_count: int = 0, detail: str | None = None) -> None:
    """Record an operator-safe description of an actual retrieval attempt."""
    if trace is None:
        return
    item = {"channel": channel, "system": system, "status": status, "result_count": result_count,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
            # Monotonic time is converted into a safe relative offset before
            # persistence. It lets the trace UI accurately show parallel work.
            "started_at_ms": round(started_at * 1000)}
    if detail:
        item["detail"] = detail
    trace.append(item)
    metrics.observe_retrieval(channel, status, item["duration_ms"])


def query_documents(db: Session, query: str, kb_ids: list[str], limit: int,
                    trace: list[dict] | None = None, plan: RetrievalPlan | None = None,
                    warnings: list[dict] | None = None) -> RetrievalEvidence:
    plan = plan or rule_plan(query, policy_from_config(None), limit).plan
    if plan.legal_context and plan.legal_context.ambiguous_context and not plan.include_historical:
        detail = plan.legal_context.ambiguity_reason or "The legal instrument or provision context is ambiguous."
        if warnings is not None:
            warnings.append({
                "code": "AMBIGUOUS_LEGAL_CONTEXT",
                "detail": detail,
                "candidate_instrument_ids": plan.legal_context.candidate_instrument_ids,
            })
        _append_retrieval_trace(trace, channel="legal_resolver", system="PostgreSQL legal registry", status="ambiguous",
                                started_at=time.monotonic(), result_count=0, detail=detail)
        return RetrievalEvidence([], [], [], [])
    channels: list[RetrievalEvidence] = []
    db_channels = {
        RetrievalChannel.EXACT: lambda session: query_exact_documents(session, plan, kb_ids, limit, trace),
        RetrievalChannel.VECTOR: lambda session: query_database_vectors(session, query, kb_ids, limit, trace, plan),
        RetrievalChannel.FULLTEXT: lambda session: query_database_chunks(session, query, kb_ids, limit, trace, plan),
        RetrievalChannel.GRAPH: lambda session: query_database_graph(session, query, kb_ids, limit, trace, plan),
    }
    for channel in (RetrievalChannel.EXACT, RetrievalChannel.VECTOR, RetrievalChannel.FULLTEXT, RetrievalChannel.GRAPH):
        if channel not in plan.channels:
            _append_retrieval_trace(trace, channel=channel.value, system="planner", status="skipped",
                                    started_at=time.monotonic(), detail="not selected by retrieval plan")
    engine = LightRAGRetrievalEngine()
    if RetrievalChannel.LIGHTRAG not in plan.channels:
        _append_retrieval_trace(trace, channel="lightrag", system="planner", status="skipped", started_at=time.monotonic(),
                                detail="not selected by retrieval plan")
    elif not engine.enabled or len(kb_ids) != 1:
        _append_retrieval_trace(trace, channel="lightrag", system="LightRAG", status="skipped", started_at=time.monotonic(),
                                detail="not configured" if not engine.enabled else "requires one Knowledge Base")
    else:
        db_channels[RetrievalChannel.LIGHTRAG] = lambda _: _query_lightrag(engine, query, kb_ids, limit, trace)
    futures = {}
    channel_results = {}
    with ThreadPoolExecutor(max_workers=max(1, len(db_channels))) as executor:
        for channel, callback in db_channels.items():
            if channel in plan.channels:
                futures[executor.submit(_run_retrieval_channel, callback, channel != RetrievalChannel.LIGHTRAG)] = channel
        for future in as_completed(futures):
            try:
                value = future.result()
                if value is not None:
                    channel_results[futures[future]] = value
            except Exception as exc:
                channel = futures[future]
                _append_retrieval_trace(trace, channel=channel.value, system="executor", status="unavailable",
                                        started_at=time.monotonic(), detail=str(exc))
    channels = [channel_results[channel] for channel in (RetrievalChannel.EXACT, RetrievalChannel.VECTOR, RetrievalChannel.FULLTEXT, RetrievalChannel.GRAPH, RetrievalChannel.LIGHTRAG) if channel in channel_results]

    if plan.legal_context and plan.legal_context.excluded_document_ids and not plan.include_historical:
        excluded = set(plan.legal_context.excluded_document_ids)
        removed, rebuilt = 0, []
        for channel_evidence in channels:
            kept = [source for source in channel_evidence.sources if source.get("document_id") not in excluded]
            removed += len(channel_evidence.sources) - len(kept)
            rebuilt.append(RetrievalEvidence(kept, channel_evidence.entities, channel_evidence.relationships, channel_evidence.paths, channel_evidence.answer))
        channels = rebuilt
        _append_retrieval_trace(trace, channel="legal_validity_filter", system="PostgreSQL legal registry", status="used",
                                started_at=time.monotonic(), result_count=removed,
                                detail=f"removed {removed} superseded/repealed source(s)")

    if plan.legal_context and plan.legal_context.preferred_document_ids and not plan.include_historical:
        preferred = set(plan.legal_context.preferred_document_ids)
        removed, rebuilt = 0, []
        for channel_evidence in channels:
            kept = [source for source in channel_evidence.sources if source.get("document_id") in preferred]
            removed += len(channel_evidence.sources) - len(kept)
            rebuilt.append(RetrievalEvidence(kept, channel_evidence.entities, channel_evidence.relationships,
                                              channel_evidence.paths, channel_evidence.answer))
        channels = rebuilt
        _append_retrieval_trace(trace, channel="legal_context_filter", system="PostgreSQL legal registry", status="used",
                                started_at=time.monotonic(), result_count=removed,
                                detail="preferred current consolidated instrument")

    if plan.intent == "legal_provision" and plan.legal_context and plan.legal_context.provision_refs:
        refs = parse_provision_refs(" ".join(plan.legal_context.provision_refs))
        removed, rebuilt = 0, []
        for channel_evidence in channels:
            kept = [
                source for source in channel_evidence.sources
                if any(
                    provision_number_matches(ref["number"], source.get("section_number"))
                    and (not source.get("section_kind") or source.get("section_kind") == ref["kind"])
                    for ref in refs
                )
            ]
            removed += len(channel_evidence.sources) - len(kept)
            rebuilt.append(RetrievalEvidence(kept, channel_evidence.entities, channel_evidence.relationships,
                                              channel_evidence.paths, channel_evidence.answer))
        channels = rebuilt
        _append_retrieval_trace(trace, channel="provision_filter", system="PostgreSQL legal registry", status="used",
                                started_at=time.monotonic(), result_count=removed,
                                detail="kept only requested provision evidence")

    channels = _prefer_consolidated_body_sources(db, channels, plan, trace)

    document_ids = {source["document_id"] for channel_evidence in channels for source in channel_evidence.sources}
    instruments_by_document = _legal_instruments_by_document(db, document_ids)
    legal_meta = {doc_id: {"authority_level": row.authority_level, "status": row.status, "effective_from": row.effective_from}
                 for doc_id, row in instruments_by_document.items()}
    fused = fuse_evidence(*channels, limit=limit, legal_meta=legal_meta, legal_context=plan.legal_context,
                          authority_weight=plan.authority_weight, status_weight=plan.status_weight, recency_weight=plan.recency_weight)
    evidence = rerank_evidence(query, fused, limit, trace, plan.rerank_enabled)
    if evidence.sources is not fused.sources:
        # The cross-encoder reranker has no legal awareness and can drop the
        # current-version guarantee fuse_evidence already applied; restore it
        # from the pre-rerank ranking, still capped at `limit`.
        guaranteed_sources = _apply_current_version_guarantee(evidence.sources, plan.legal_context, limit, fused.sources)
        if guaranteed_sources != evidence.sources:
            sources = [{**item, "citation_id": f"S{index}"} for index, item in enumerate(guaranteed_sources, 1)]
            evidence = RetrievalEvidence(sources, evidence.entities, evidence.relationships, evidence.paths, evidence.answer)

    if instruments_by_document and evidence.sources:
        conflict_warnings = validate_legal_evidence(db, evidence, plan, instruments_by_document)
        if conflict_warnings:
            if warnings is not None:
                warnings.extend(conflict_warnings)
            _append_retrieval_trace(trace, channel="conflict_check", system="Legal registry", status="used",
                                    started_at=time.monotonic(), result_count=len(conflict_warnings),
                                    detail="version/provision conflicts detected")
        _decorate_sources_with_legal_metadata(evidence.sources, instruments_by_document)

    if evidence.sources:
        started_at = time.monotonic()
        try:
            evidence.answer = OpenRouterClient().answer_from_sources(query, evidence.sources)
            _append_retrieval_trace(trace, channel="answer_generation", system="OpenRouter LLM", status="used",
                                    started_at=started_at, result_count=len(evidence.sources), detail="cited answer")
        except RuntimeError as exc:
            evidence.answer = None
            _append_retrieval_trace(trace, channel="answer_generation", system="OpenRouter LLM", status="unavailable",
                                    started_at=started_at, detail=str(exc))
        _validate_answer_citations(evidence, warnings)
    return evidence


def _run_retrieval_channel(callback, needs_db: bool):
    session = SessionLocal() if needs_db else None
    try:
        return callback(session)
    finally:
        if session is not None:
            session.close()


def _query_lightrag(engine: LightRAGRetrievalEngine, query: str, kb_ids: list[str], limit: int,
                    trace: list[dict] | None = None) -> RetrievalEvidence:
    started_at = time.monotonic()
    try:
        value = engine.query(query, kb_ids, limit)
        _append_retrieval_trace(trace, channel="lightrag", system="LightRAG", status="used",
                                started_at=started_at, result_count=len(value.sources), detail="mix retrieval")
        return value
    except RuntimeError as exc:
        _append_retrieval_trace(trace, channel="lightrag", system="LightRAG", status="unavailable",
                                started_at=started_at, detail=str(exc))
        return RetrievalEvidence([], [], [], [])


def _apply_published_filter(rows, plan: RetrievalPlan | None):
    if not plan:
        return rows
    if plan.published_from:
        rows = rows.filter(Document.published_at >= plan.published_from)
    if plan.published_to:
        rows = rows.filter(Document.published_at < plan.published_to)
    return rows


def _apply_legal_filter(rows, plan: RetrievalPlan | None):
    """Drop rows for documents the legal resolver marked repealed/superseded at
    the resolved as-of date; a no-op unless the query actually touched a legal KB."""
    if not plan or plan.include_historical or not plan.legal_context or not plan.legal_context.excluded_document_ids:
        return rows
    return rows.filter(Document.id.notin_(plan.legal_context.excluded_document_ids))


def _apply_provision_filter(rows, plan: RetrievalPlan | None):
    """Restrict legal provision retrieval to the requested article/section.

    A bare number such as 6 appears in every amendment file.  Once the legal
    resolver selects an instrument, returning unrelated sections from that
    instrument is still unsafe because the answer model may cite them.
    """
    if not plan or plan.intent != "legal_provision" or not plan.legal_context or not plan.legal_context.provision_refs:
        return rows
    refs = parse_provision_refs(" ".join(plan.legal_context.provision_refs))
    if not refs:
        return rows
    predicates = [
        (DocumentChunk.section_kind == ref["kind"]) & (DocumentChunk.section_number == ref["number"])
        for ref in refs
    ]
    return rows.filter(or_(*predicates))


def _prefer_consolidated_body_sources(db: Session, channels: list[RetrievalEvidence], plan: RetrievalPlan,
                                      trace: list[dict] | None = None) -> list[RetrievalEvidence]:
    """Select the code-body occurrence when a consolidated file also contains
    the enabling Act and amendment footnotes with the same article number.

    The official corpus intentionally preserves those appendices for
    provenance.  They must not compete with the first occurrence in the
    consolidated code body for a normal ``ประมวลกฎหมาย ... มาตรา`` query.
    """
    if not plan.legal_context or not plan.legal_context.preferred_document_ids or not plan.legal_context.provision_refs:
        return channels
    refs = parse_provision_refs(" ".join(plan.legal_context.provision_refs))
    preferred = set(plan.legal_context.preferred_document_ids)
    body_starts: dict[str, int] = {}
    for document_id in preferred:
        value = db.query(func.min(DocumentChunk.chunk_index)).filter(
            DocumentChunk.document_id == document_id, DocumentChunk.section_kind == "หมวด"
        ).scalar()
        if value is not None:
            body_starts[document_id] = int(value)
    if not body_starts:
        return channels

    # Pick one canonical body chunk per requested provision.  We deliberately
    # compare chunk indexes globally across channels because vector, FTS and
    # LightRAG may return the same provision from different routes.
    chosen_indices: dict[tuple[str, str], int] = {}
    for channel in channels:
        for source in channel.sources:
            document_id = source.get("document_id")
            chunk_index = source.get("chunk_index")
            if document_id not in body_starts or chunk_index is None or int(chunk_index) < body_starts[document_id]:
                continue
            for ref in refs:
                if provision_number_matches(ref["number"], source.get("section_number")) and (
                    not source.get("section_kind") or source.get("section_kind") == ref["kind"]
                ):
                    key = (document_id, ref["number"])
                    idx = int(chunk_index)
                    chosen_indices[key] = min(chosen_indices.get(key, idx), idx)

    if not chosen_indices:
        return channels
    rebuilt, removed = [], 0
    # Keep the earliest matching body chunk per requested number.  This also
    # removes the same-number enabling-Act and amendment appendix chunks.
    for channel in channels:
        kept = []
        for source in channel.sources:
            if any(
                source.get("document_id") == document_id
                and provision_number_matches(number, source.get("section_number"))
                and source.get("chunk_index") == chosen_indices.get((document_id, number))
                for document_id, number in chosen_indices
            ):
                kept.append(source)
            elif source.get("document_id") in preferred:
                removed += 1
        rebuilt.append(RetrievalEvidence(kept, channel.entities, channel.relationships, channel.paths, channel.answer))
    _append_retrieval_trace(trace, channel="legal_body_selector", system="PostgreSQL legal registry", status="used",
                            started_at=time.monotonic(), result_count=removed,
                            detail="selected earliest occurrence in consolidated code body")
    return rebuilt


def query_database_vectors(db: Session, query: str, kb_ids: list[str], limit: int,
                           trace: list[dict] | None = None, plan: RetrievalPlan | None = None) -> RetrievalEvidence:
    started_at = time.monotonic()
    if db.get_bind().dialect.name != "postgresql":
        _append_retrieval_trace(trace, channel="semantic_vector", system="PostgreSQL + pgvector", status="skipped",
                                started_at=started_at, detail="PostgreSQL is not the active database")
        return RetrievalEvidence([], [], [], [])
    client = OpenRouterClient()
    if not client.embeddings_enabled:
        _append_retrieval_trace(trace, channel="semantic_vector", system="PostgreSQL + pgvector", status="skipped",
                                started_at=started_at, detail="embedding provider is not configured")
        return RetrievalEvidence([], [], [], [])
    try:
        query_vector = client.embed_texts([query])[0]
    except RuntimeError as exc:
        _append_retrieval_trace(trace, channel="semantic_vector", system="OpenRouter embeddings → PostgreSQL + pgvector",
                                status="unavailable", started_at=started_at, detail=str(exc))
        return RetrievalEvidence([], [], [], [])
    distance = DocumentChunk.embedding.cosine_distance(query_vector)
    rows = db.query(DocumentChunk, Document, distance.label("distance")).join(
        Document, Document.id == DocumentChunk.document_id
    ).filter(Document.status == "completed", Document.deleted_at.is_(None), DocumentChunk.embedding.is_not(None))
    if kb_ids:
        rows = rows.filter(DocumentChunk.knowledge_base_id.in_(kb_ids))
    rows = _apply_published_filter(rows, plan)
    rows = _apply_legal_filter(rows, plan)
    rows = _apply_provision_filter(rows, plan)
    records = rows.order_by(distance).limit(limit).all()
    sources = [{"citation_id": f"S{i}", "document_id": document.id, "title": document.title,
                "chunk_id": chunk.id, "chunk_index": chunk.chunk_index, "excerpt": chunk.content[:500], "relevance": max(0.0, 1.0 - float(item_distance)),
                "section_kind": chunk.section_kind, "section_number": chunk.section_number, "section_label": chunk.section_label}
               for i, (chunk, document, item_distance) in enumerate(records, 1)]
    _append_retrieval_trace(trace, channel="semantic_vector", system="OpenRouter embeddings → PostgreSQL + pgvector",
                            status="used", started_at=started_at, result_count=len(sources), detail="cosine similarity")
    return RetrievalEvidence(sources, [], [], [])


def query_database_chunks(db: Session, query: str, kb_ids: list[str], limit: int,
                          trace: list[dict] | None = None, plan: RetrievalPlan | None = None) -> RetrievalEvidence:
    started_at = time.monotonic()
    words = [word for word in re.findall(r"[\w-]+", query.lower()) if len(word) > 1]
    base_rows = db.query(DocumentChunk, Document).join(Document, Document.id == DocumentChunk.document_id).filter(
        Document.status == "completed", Document.deleted_at.is_(None)
    )
    if kb_ids:
        base_rows = base_rows.filter(DocumentChunk.knowledge_base_id.in_(kb_ids))
    base_rows = _apply_published_filter(base_rows, plan)
    base_rows = _apply_legal_filter(base_rows, plan)
    base_rows = _apply_provision_filter(base_rows, plan)
    rows = base_rows
    if words:
        if db.get_bind().dialect.name == "postgresql":
            vector = func.to_tsvector("simple", DocumentChunk.content)
            tsquery = func.websearch_to_tsquery("simple", query)
            rows = rows.filter(vector.op("@@")(tsquery)).order_by(func.ts_rank_cd(vector, tsquery).desc())
        else:
            rows = rows.filter(or_(*[DocumentChunk.content.ilike(f"%{word}%") for word in words[:8]]))
    records = rows.limit(limit).all()
    if not records and words and db.get_bind().dialect.name == "postgresql":
        # PostgreSQL simple FTS does not segment Thai. Retain FTS as the first
        # choice, then use an explicit phrase/token fallback in the same scope.
        records = base_rows.filter(or_(*[DocumentChunk.content.ilike(f"%{word}%") for word in words[:8]])).limit(limit).all()
    sources = [{"citation_id": f"S{i}", "document_id": document.id, "title": document.title,
                "chunk_id": chunk.id, "chunk_index": chunk.chunk_index, "excerpt": chunk.content[:500], "relevance": 1.0 / i,
                "section_kind": chunk.section_kind, "section_number": chunk.section_number, "section_label": chunk.section_label}
               for i, (chunk, document) in enumerate(records, 1)]
    system = "PostgreSQL full-text search" if db.get_bind().dialect.name == "postgresql" else "SQL text fallback"
    _append_retrieval_trace(trace, channel="full_text", system=system, status="used",
                            started_at=started_at, result_count=len(sources), detail="keyword retrieval")
    return RetrievalEvidence(sources, [], [], [])


def query_exact_documents(db: Session, plan: RetrievalPlan, kb_ids: list[str], limit: int,
                          trace: list[dict] | None = None) -> RetrievalEvidence:
    started_at = time.monotonic()
    identifiers = plan.document_identifiers
    if not identifiers:
        _append_retrieval_trace(trace, channel="exact_document", system="PostgreSQL document metadata", status="used",
                                started_at=started_at, detail="no document identifier supplied")
        return RetrievalEvidence([], [], [], [])
    rows = db.query(DocumentChunk, Document).join(Document, Document.id == DocumentChunk.document_id).filter(
        Document.status == "completed", Document.deleted_at.is_(None)
    )
    if kb_ids:
        rows = rows.filter(DocumentChunk.knowledge_base_id.in_(kb_ids))
    rows = _apply_published_filter(rows, plan)
    rows = _apply_legal_filter(rows, plan)
    rows = _apply_provision_filter(rows, plan)
    predicates = []
    for identifier in identifiers:
        pattern = f"%{identifier}%"
        predicates.extend((Document.title.ilike(pattern), Document.original_filename.ilike(pattern), DocumentChunk.content.ilike(pattern)))
    records = rows.filter(or_(*predicates)).order_by(DocumentChunk.chunk_index).limit(limit).all()
    sources = [{"citation_id": f"S{i}", "document_id": document.id, "title": document.title,
                "chunk_id": chunk.id, "chunk_index": chunk.chunk_index, "excerpt": chunk.content[:500], "relevance": 1.0 / i,
                "section_kind": chunk.section_kind, "section_number": chunk.section_number, "section_label": chunk.section_label}
               for i, (chunk, document) in enumerate(records, 1)]
    _append_retrieval_trace(trace, channel="exact_document", system="PostgreSQL document metadata", status="used",
                            started_at=started_at, result_count=len(sources), detail="title, filename, and document ID lookup")
    return RetrievalEvidence(sources, [], [], [])


def _verified_relationships(db: Session, kb_ids: list[str]):
    rows = db.query(Relationship).filter(
        Relationship.deleted_at.is_(None),
        (Relationship.is_legal.is_(False)) | (Relationship.review_status == "verified"),
    )
    if kb_ids:
        rows = rows.filter(Relationship.knowledge_base_id.in_(kb_ids))
    return rows


def _global_graph_evidence(db: Session, kb_ids: list[str], limit: int, plan: RetrievalPlan) -> RetrievalEvidence:
    """Return representative cited edges from several connected components."""
    relationships = _verified_relationships(db, kb_ids).order_by(Relationship.source_count.desc(), Relationship.created_at.desc()).limit(max(limit * 10, 50)).all()
    if not relationships:
        return RetrievalEvidence([], [], [], [])
    parent: dict[str, str] = {}
    def find(value: str) -> str:
        parent.setdefault(value, value)
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]
    def union(first: str, second: str) -> None:
        first, second = find(first), find(second)
        if first != second:
            parent[second] = first
    for relationship in relationships:
        union(relationship.source_entity_id, relationship.target_entity_id)
    selected: list[Relationship] = []
    seen_components: set[str] = set()
    for relationship in relationships:
        component = find(relationship.source_entity_id)
        if component not in seen_components:
            selected.append(relationship); seen_components.add(component)
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        selected.extend(item for item in relationships if item not in selected)
    selected = selected[:limit]
    node_ids = {item.source_entity_id for item in selected} | {item.target_entity_id for item in selected}
    nodes = db.query(Entity).filter(Entity.id.in_(node_ids), Entity.deleted_at.is_(None)).all()
    edges = [{"id": item.id, "source": item.source_entity_id, "target": item.target_entity_id, "type": item.relationship_type} for item in selected]
    sources = relationship_sources(db, selected, plan)[:limit]
    for source in sources:
        source.pop("_relationship_id", None)
    return RetrievalEvidence(sources, [{"id": item.id, "name": item.name, "type": item.entity_type} for item in nodes], edges, edges)


def query_database_graph(db: Session, query: str, kb_ids: list[str], limit: int,
                         trace: list[dict] | None = None, plan: RetrievalPlan | None = None) -> RetrievalEvidence:
    """Use local entity traversal or bounded global graph evidence as planned."""
    started_at = time.monotonic()
    plan = plan or rule_plan(query, policy_from_config(None), limit).plan
    if plan.graph_scope == "global":
        evidence = _global_graph_evidence(db, kb_ids, limit, plan)
        _append_retrieval_trace(trace, channel="graph", system="PostgreSQL graph tables", status="used",
                                started_at=started_at, result_count=len(evidence.sources), detail="global representative relationship evidence")
        return evidence
    subject = next(iter(plan.entity_subjects), query)
    entity = resolve_entity(db, kb_ids, subject)
    if not entity:
        _append_retrieval_trace(trace, channel="graph", system="PostgreSQL graph tables", status="used",
                                started_at=started_at, detail="no scoped entity matched")
        return RetrievalEvidence([], [], [], [])
    store = Neo4jGraphStore()
    accelerator = "postgresql fallback"
    relationship_ids: list[str] = []
    node_ids: list[str] = []
    if store.enabled:
        try:
            result = store.traverse(entity.id, entity.knowledge_base_id, plan.graph_depth, max(limit * 10, 50))
            relationship_ids, node_ids = result.get("relationship_ids", []), result.get("node_ids", [])
            if relationship_ids:
                accelerator = "neo4j accelerator → PostgreSQL evidence"
        except (httpx.HTTPError, RuntimeError, ValueError):
            relationship_ids = []
    if relationship_ids:
        relationships = _verified_relationships(db, [entity.knowledge_base_id]).filter(Relationship.id.in_(relationship_ids)).all()
        node_rows = db.query(Entity).filter(Entity.id.in_(node_ids), Entity.deleted_at.is_(None)).all()
        graph = {"nodes": [{"id": row.id, "name": row.name, "type": row.entity_type} for row in node_rows],
                 "edges": [{"id": row.id, "source": row.source_entity_id, "target": row.target_entity_id, "type": row.relationship_type} for row in relationships]}
    else:
        graph = entity_graph(db, entity, plan.graph_depth)
        relationships = _verified_relationships(db, [entity.knowledge_base_id]).filter(Relationship.id.in_([edge["id"] for edge in graph["edges"]])).all()
    sources = relationship_sources(db, relationships, plan)[:limit]
    for source in sources:
        source.pop("_relationship_id", None)
    _append_retrieval_trace(trace, channel="graph", system=accelerator, status="used",
                            started_at=started_at, result_count=len(sources), detail="verified/manual bounded traversal")
    return RetrievalEvidence(sources, graph["nodes"], graph["edges"], graph["edges"])


_LEGAL_STATUS_FACTORS = {
    "in_force": 1.0, "amended": 0.85, "not_yet_effective": 0.3, "unknown": 0.5, "superseded": 0.1, "repealed": 0.1,
}


def _legal_instruments_by_document(db: Session, document_ids: set[str]) -> dict[str, LegalInstrument]:
    if not document_ids:
        return {}
    rows = db.query(LegalInstrument).filter(LegalInstrument.document_id.in_(document_ids)).all()
    return {row.document_id: row for row in rows}


def _apply_current_version_guarantee(kept_sources: list[dict], legal_context: LegalContext | None, limit: int,
                                     pool: list[dict]) -> list[dict]:
    """`kept_sources` is already ranked and capped at `limit`. If a document the
    resolver marked as a current legal version is missing from it, backfill the
    first matching source found in `pool` (a superset, e.g. the pre-rerank
    ranking) and trim back down to `limit` -- the result never exceeds `limit`
    even when several documents need backfilling. A no-op without legal_context."""
    if not legal_context or not legal_context.current_version_ids:
        return kept_sources
    present = {source["document_id"] for source in kept_sources}
    missing = [doc_id for doc_id in legal_context.current_version_ids if doc_id not in present]
    if not missing:
        return kept_sources
    missing_set, seen, backfill = set(missing), set(), []
    for source in pool:
        document_id = source["document_id"]
        if document_id in missing_set and document_id not in seen:
            seen.add(document_id)
            backfill.append(source)
    backfill = backfill[:limit]
    return kept_sources[:max(0, limit - len(backfill))] + backfill


def fuse_evidence(*channels: RetrievalEvidence, limit: int, legal_meta: dict[str, dict] | None = None,
                  legal_context: LegalContext | None = None, authority_weight: float = 0.0,
                  status_weight: float = 0.0, recency_weight: float = 0.0) -> RetrievalEvidence:
    """Use reciprocal-rank fusion across semantic, FTS, and graph channels, then
    apply an authority/status/recency boost for any document with a legal
    registry entry. A document absent from legal_meta gets boost 0, so a
    Knowledge Base with no legal instruments scores exactly as before."""
    candidates: dict[str, dict] = {}
    for channel in channels:
        for rank, source in enumerate(channel.sources, 1):
            # A single document can contribute several relevant passages. Keep
            # each chunk as independent evidence so answer generation receives
            # the passage that actually addresses the question.
            key = f"{source['document_id']}:{source['chunk_id']}"
            score = 1.0 / (60 + rank)
            candidate = candidates.setdefault(key, {"score": 0.0, "source": source})
            candidate["score"] += score
            if source.get("relevance", 0.0) > candidate["source"].get("relevance", 0.0):
                candidate["source"] = source

    legal_meta = legal_meta or {}
    dated_documents = sorted(
        (doc_id for doc_id, meta in legal_meta.items() if meta.get("effective_from")),
        key=lambda doc_id: legal_meta[doc_id]["effective_from"], reverse=True,
    )
    recency_rank = {doc_id: 1.0 - (index / max(len(dated_documents) - 1, 1)) for index, doc_id in enumerate(dated_documents)}
    for candidate in candidates.values():
        meta = legal_meta.get(candidate["source"]["document_id"])
        if not meta:
            continue
        boost = (
            authority_weight * (meta["authority_level"] / 100)
            + status_weight * _LEGAL_STATUS_FACTORS.get(meta["status"], 0.5)
            + recency_weight * recency_rank.get(candidate["source"]["document_id"], 0.5)
        )
        candidate["score"] *= (1 + boost)

    ranked = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)
    ranked_sources = [{**item["source"], "relevance": round(item["score"], 6)} for item in ranked]

    # Guaranteed representation: a "current version" document must appear even
    # if raw similarity pushed it out of the top-`limit`, so an amended act
    # never gets silently displaced by an unrelated FAQ hit. The result never
    # exceeds `limit`, even when several documents need backfilling.
    ordered_sources = _apply_current_version_guarantee(ranked_sources[:limit], legal_context, limit, ranked_sources)
    sources = [{**item, "citation_id": f"S{index}"} for index, item in enumerate(ordered_sources, 1)]
    answer = next((channel.answer for channel in channels if channel.answer), None)
    entities = [item for channel in channels for item in channel.entities]
    relationships = [item for channel in channels for item in channel.relationships]
    paths = [item for channel in channels for item in channel.paths]
    return RetrievalEvidence(sources, entities, relationships, paths, answer)


def rerank_evidence(query: str, evidence: RetrievalEvidence, limit: int,
                    trace: list[dict] | None = None, enabled: bool = True) -> RetrievalEvidence:
    """Optionally apply a cross-encoder reranker after deterministic fusion."""
    client = OpenRouterClient()
    if not enabled or not client.reranker_enabled or not evidence.sources:
        _append_retrieval_trace(trace, channel="rerank", system="OpenRouter reranker", status="skipped", started_at=time.monotonic(),
                                detail="disabled by retrieval policy" if not enabled else ("not configured" if not client.reranker_enabled else "no candidates"))
        return evidence
    started_at = time.monotonic()
    candidates = evidence.sources[:get_settings().rerank_candidate_limit]
    try:
        ranked = client.rerank(query, [item["excerpt"] for item in candidates], limit)
    except RuntimeError as exc:
        _append_retrieval_trace(trace, channel="rerank", system="OpenRouter reranker", status="unavailable",
                                started_at=started_at, detail=str(exc))
        return evidence
    ordered = []
    for index, relevance in ranked:
        if 0 <= index < len(candidates):
            ordered.append({**candidates[index], "relevance": relevance})
    if not ordered:
        _append_retrieval_trace(trace, channel="rerank", system="OpenRouter reranker", status="used",
                                started_at=started_at, detail="no ranked candidates returned")
        return evidence
    sources = [{**source, "citation_id": f"S{index}"} for index, source in enumerate(ordered[:limit], 1)]
    _append_retrieval_trace(trace, channel="rerank", system="OpenRouter reranker", status="used",
                            started_at=started_at, result_count=len(sources), detail="cross-encoder rerank")
    return RetrievalEvidence(sources, evidence.entities, evidence.relationships, evidence.paths, evidence.answer)


_LEGAL_STATUS_LABELS_TH = {
    "in_force": "บังคับใช้", "amended": "แก้ไขเพิ่มเติมแล้ว", "superseded": "ถูกแทนที่",
    "repealed": "ถูกยกเลิก", "not_yet_effective": "ยังไม่มีผลบังคับใช้", "unknown": "ไม่ทราบสถานะ",
}


def validate_legal_evidence(db: Session, evidence: RetrievalEvidence, plan: RetrievalPlan | None,
                            instruments_by_document: dict[str, LegalInstrument]) -> list[dict]:
    """Detect same-provision version conflicts and provision-level overrides in
    the fused evidence before the LLM sees it; mutates evidence.sources in place."""
    if not evidence.sources or not plan:
        return []
    warnings: list[dict] = []

    instrument_ids = [row.id for row in instruments_by_document.values()]
    relations = db.query(LegalInstrumentRelation).filter(
        LegalInstrumentRelation.target_instrument_id.in_(instrument_ids), LegalInstrumentRelation.review_status == "verified",
        LegalInstrumentRelation.target_provision.is_not(None), LegalInstrumentRelation.relation.in_(("REPEALS", "SUPERSEDES", "AMENDS")),
    ).all() if instrument_ids else []
    relations_by_target: dict[str, list[LegalInstrumentRelation]] = {}
    for relation in relations:
        relations_by_target.setdefault(relation.target_instrument_id, []).append(relation)

    def overriding_relation(instrument: LegalInstrument, section_number: str) -> LegalInstrumentRelation | None:
        return next((relation for relation in relations_by_target.get(instrument.id, [])
                    if provision_number_matches(relation.target_provision, section_number)), None)

    # 1. Same family + same section kind + same provision number, appearing in
    # MORE THAN ONE DOCUMENT: a verified provision-level edge is the strongest
    # signal of which one is current; fall back to legal_context, then to
    # whichever source ranked first. section_kind is part of the key because a
    # bare number collides across heading kinds (มาตรา 2 and หมวด 2 are not the
    # same provision); requiring >1 document_id avoids treating a long
    # provision's own sub-split chunks (same document) as a version conflict.
    groups: dict[tuple, list[dict]] = {}
    for source in evidence.sources:
        instrument = instruments_by_document.get(source["document_id"])
        section_number = source.get("section_number")
        if not instrument or not instrument.family_id or not section_number:
            continue
        groups.setdefault((instrument.family_id, source.get("section_kind"), section_number), []).append(source)
    current_ids = set(plan.legal_context.current_version_ids) if plan.legal_context else set()
    to_remove: set[int] = set()
    for (_, _, section_number), group in groups.items():
        group_document_ids = {source["document_id"] for source in group}
        if len(group_document_ids) < 2:
            continue
        overridden_ids = {id(source) for source in group
                          if overriding_relation(instruments_by_document[source["document_id"]], section_number)}
        current_in_group = group_document_ids & current_ids
        if overridden_ids and len(overridden_ids) < len(group):
            keep_candidates = [source for source in group if id(source) not in overridden_ids]
        elif current_in_group and current_in_group != group_document_ids:
            # A strict subset is current; a full or empty overlap is ambiguous.
            keep_candidates = [source for source in group if source["document_id"] in current_in_group]
        elif plan.legal_context is not None:
            # The resolver evaluated this Knowledge Base and could not
            # disambiguate without a reviewed relationship; trust that
            # ambiguity rather than guessing which version to drop.
            continue
        else:
            keep_candidates = group
        keep = keep_candidates[0]
        for source in group:
            if source is keep:
                continue
            if not plan.include_historical:
                to_remove.add(id(source))
            warnings.append({
                "code": "SUPERSEDED_VERSION_REMOVED" if not plan.include_historical else "SUPERSEDED_VERSION_PRESENT",
                "detail": f"{source.get('title')} {section_number} is superseded by the current version",
            })
    if to_remove:
        evidence.sources[:] = [source for source in evidence.sources if id(source) not in to_remove]

    # 2. Provision-level repeal/supersede/amend that still targets a chunk left
    # in evidence (e.g. the whole instrument otherwise remains in force).
    for source in evidence.sources:
        instrument = instruments_by_document.get(source["document_id"])
        section_number = source.get("section_number")
        if not instrument or not section_number:
            continue
        relation = overriding_relation(instrument, section_number)
        if relation and relation.relation in {"REPEALS", "SUPERSEDES"}:
            warnings.append({
                "code": "PROVISION_REPEALED",
                "detail": f"{instrument.official_title or source.get('title')} {section_number} was {relation.relation.lower()}; verify against the amending instrument",
            })
        elif relation and relation.relation == "AMENDS":
            warnings.append({
                "code": "PROVISION_AMENDED",
                "detail": f"{instrument.official_title or source.get('title')} {section_number} was amended; verify against the amending instrument",
            })

    # 3. Unverified validity: source document has no resolvable status.
    for source in evidence.sources:
        instrument = instruments_by_document.get(source["document_id"])
        if instrument and instrument.status == "unknown":
            warnings.append({
                "code": "UNVERIFIED_VALIDITY",
                "detail": f"{instrument.official_title or source.get('title')} has no confirmed effective date",
            })
    return warnings


def _decorate_sources_with_legal_metadata(sources: list[dict], instruments_by_document: dict[str, LegalInstrument]) -> None:
    """Attach version/status metadata to each source so citations and the LLM
    prompt can tell an in-force provision from a repealed or amended one."""
    for source in sources:
        instrument = instruments_by_document.get(source["document_id"])
        if not instrument:
            continue
        source["document_status"] = instrument.status
        source["authority_level"] = instrument.authority_level
        source["kind"] = instrument.kind
        source["version_label"] = instrument.version_label
        source["effective_from"] = instrument.effective_from.isoformat() if instrument.effective_from else None
        source["effective_to"] = instrument.effective_to.isoformat() if instrument.effective_to else None
        source["version_date"] = instrument.version_date.isoformat() if instrument.version_date else None
        source["legal_work_key"] = instrument.legal_work_key
        source["document_class"] = instrument.document_class
        source["source_uri"] = instrument.source_uri
        source["source_reference"] = instrument.source_reference
        source["provenance"] = {"origin": "legal_registry", "review_status": instrument.review_status,
                                 "document_id": instrument.document_id, "source_uri": instrument.source_uri,
                                 "source_reference": instrument.source_reference}
        parts = [instrument.official_title or source.get("title"),
                f"สถานะ: {_LEGAL_STATUS_LABELS_TH.get(instrument.status, instrument.status)}"]
        if source.get("section_label"):
            parts.append(source["section_label"])
        if instrument.effective_from:
            parts.append(f"มีผล: {instrument.effective_from.isoformat()}")
        source["legal_label"] = " | ".join(str(part) for part in parts if part)


_CITATION_RE = re.compile(r"\[(S\d+)\]")


def _validate_answer_citations(evidence: RetrievalEvidence, warnings: list[dict] | None = None) -> None:
    """Keep only evidence IDs actually cited by the generated answer.

    Retrieval returns a candidate pool, while the answer is the user-visible
    claim set.  Persisting all candidates as claim citations made unrelated
    chunks look like proof.  A missing/unknown citation is fail-closed.
    """
    answer = (evidence.answer or "").strip()
    if not answer:
        return
    cited_ids = set(_CITATION_RE.findall(answer))
    available = {str(source.get("citation_id")): source for source in evidence.sources}
    valid_ids = cited_ids & available.keys()
    unknown_ids = sorted(cited_ids - available.keys())
    if not valid_ids:
        if warnings is not None:
            warnings.append({
                "code": "ANSWER_CITATIONS_MISSING",
                "detail": "The generated answer did not cite a valid supplied evidence ID.",
                "unknown_citation_ids": unknown_ids,
            })
        evidence.answer = None
        evidence.sources = []
        return
    if unknown_ids:
        if warnings is not None:
            warnings.append({
                "code": "ANSWER_CITATION_ID_INVALID",
                "detail": "The generated answer referenced an evidence ID that was not supplied.",
                "unknown_citation_ids": unknown_ids,
            })
        evidence.answer = None
        evidence.sources = []
        return
    evidence.sources = [available[citation_id] for citation_id in sorted(valid_ids, key=lambda value: int(value[1:]))]


def compose_cited_answer(evidence: RetrievalEvidence, warnings: list[dict] | None = None) -> str:
    if warnings and any(item.get("code") == "AMBIGUOUS_LEGAL_CONTEXT" for item in warnings):
        return "ยังไม่สามารถตอบได้อย่างปลอดภัย เนื่องจากพบหลายฉบับที่อาจตรงกับมาตราที่ระบุ โปรดระบุชื่อฉบับกฎหมายหรือวันที่มีผลบังคับใช้ให้ชัดเจน"
    if warnings and any(item.get("code") in {"ANSWER_CITATIONS_MISSING", "ANSWER_CITATION_ID_INVALID"} for item in warnings):
        return "ยังไม่สามารถสร้างคำตอบที่มีหลักฐานอ้างอิงที่ตรวจสอบได้จากคำขอนี้"
    if not evidence.sources:
        return "Insufficient evidence was found in the selected knowledge bases."
    citations = " ".join(f"[{source['citation_id']}]" for source in evidence.sources)
    answer = (evidence.answer or "").strip()
    if not answer:
        return f"Supporting knowledge was found: {citations}"
    if not re.search(r"\[S\d+\]", answer):
        return f"{answer}\n\nSources: {citations}"
    return answer


def build_query_result(db: Session, query: str, kb_ids: list[str], max_sources: int, token_id: str | None = None, query_filters=None) -> dict:
    retrieval_trace: list[dict] = []
    legal_warnings: list[dict] = []
    decision = build_retrieval_plan(db, query, kb_ids, max_sources, query_filters, retrieval_trace)
    evidence = query_documents(db, query, kb_ids, decision.plan.max_sources, retrieval_trace, decision.plan, legal_warnings)
    intent = decision.plan.intent
    answer = compose_cited_answer(evidence, legal_warnings)
    legal_context = decision.plan.legal_context.model_dump(mode="json") if decision.plan.legal_context else None
    legal_status = "not_applicable"
    if legal_context is not None:
        legal_status = "insufficient" if not evidence.sources else ("partial" if legal_warnings else "verified")
    answer_contract = {"status": legal_status, "as_of_date": decision.plan.as_of_date.isoformat() if decision.plan.as_of_date else None,
                       "warnings": legal_warnings, "citation_required": bool(evidence.sources),
                       "claim_citations": [source.get("citation_id") for source in evidence.sources]}
    metrics.observe_query_outcome("insufficient_evidence" if not evidence.sources else "evidence_found")
    result = {"status": "success", "result_id": "", "answer": answer,
              "insufficient_evidence": not bool(evidence.sources), "entities": evidence.entities,
              "relationships": evidence.relationships, "paths": evidence.paths, "sources": evidence.sources,
              "warnings": legal_warnings, "metadata": {"knowledge_base_ids": kb_ids, "retrieval_strategy": intent,
                                                "retrieval_plan": decision.plan.model_dump(mode="json"),
                                                "planner_policy_version": decision.policy_version,
                                                "retrieval_trace": retrieval_trace,
                                                "legal_context": legal_context,
                                                "answer_contract": answer_contract,
                                                # Safe observability summaries. Full prompts, provider
                                                # payloads, and document bodies are never persisted.
                                                "query_preview": query[:500] if get_settings().log_query_text else None,
                                                "query_length": len(query),
                                                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                                                "answer_preview": answer[:800] if get_settings().log_query_text else None,
                                                "citation_ids": [source.get("citation_id") for source in evidence.sources],
                                                "filter_summary": query_filters.model_dump(mode="json") if query_filters else {},
                                                "response_summary": {"status": "success", "insufficient_evidence": not bool(evidence.sources),
                                                                     "source_count": len(evidence.sources), "entity_count": len(evidence.entities),
                                                                     "relationship_count": len(evidence.relationships)}}}
    saved = QueryResult(token_key_id=token_id, result_json=result, expires_at=datetime.utcnow() + timedelta(minutes=30))
    db.add(saved); db.flush(); result["result_id"] = saved.id; saved.result_json = result; db.commit()
    return result
