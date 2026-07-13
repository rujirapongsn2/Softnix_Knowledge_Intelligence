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
from .models import Document, DocumentChunk, Entity, EntitySource, GraphProjectionEvent, KnowledgeBase, ProcessingJob, QueryResult, Relationship, RelationshipSource
from .openrouter import OpenRouterClient
from .observability import metrics
from .planner import RetrievalChannel, RetrievalPlan, PlannerDecision, apply_llm_plan, intersect_policies, policy_from_config, rule_plan
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
    "ISSUED_UNDER", "IMPLEMENTS", "AMENDS", "REPEALS", "REFERS_TO", "GOVERNED_BY",
}


def _legal_value(item, *keys: str) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    return str(next((item.get(key) for key in keys if item.get(key)), "")).strip()


def _legal_evidence(item, fallback: str) -> str:
    return (_legal_value(item, "evidence_quote", "excerpt", "text", "description") or fallback)[:5000]


def legal_metadata_v2(document: Document) -> dict:
    """Normalize legacy legal metadata without discarding a human-edited value."""
    metadata = document.legal_metadata or {}
    if metadata.get("schema_version") == 2:
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
        },
        "provisions": provisions,
        "parties": metadata.get("parties") if isinstance(metadata.get("parties"), list) else [],
        "obligations": metadata.get("obligations") if isinstance(metadata.get("obligations"), list) else [],
        "rights": metadata.get("rights") if isinstance(metadata.get("rights"), list) else [],
        "prohibitions": metadata.get("prohibitions") if isinstance(metadata.get("prohibitions"), list) else [],
        "penalties": metadata.get("penalties") if isinstance(metadata.get("penalties"), list) else [],
        "definitions": metadata.get("definitions") if isinstance(metadata.get("definitions"), list) else [],
        "amendments": metadata.get("amendments") if isinstance(metadata.get("amendments"), list) else [],
        "references": metadata.get("references") if isinstance(metadata.get("references"), list) else [],
        "confidence": metadata.get("confidence", 0.0),
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
    for candidate in candidates:
        metadata = legal_metadata_v2(candidate)
        instrument = metadata.get("instrument") or {}
        candidate_title = _legal_value(instrument, "official_title") or candidate.title or candidate.original_filename
        if title and (canonical_entity_name(title) in canonical_entity_name(candidate_title) or canonical_entity_name(candidate_title) in canonical_entity_name(title)):
            return candidate
        if number and number == str(instrument.get("official_number") or ""):
            return candidate
    return None


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
            _, created = _upsert_legal_relationship(
                db, document, source=source, target=target, relationship_type=relationship_type,
                excerpt=_legal_evidence(reference, relationship_type), origin="ai_suggestion", review_status="suggested",
                confidence=float(reference.get("confidence") or 0.5), attributes={"reference": reference},
            )
            count += int(created)
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
    totals["suggestions"] = build_legal_cross_document_suggestions(db, knowledge_base_id)
    db.commit()
    return totals


def resolve_entity(db: Session, knowledge_base_ids: list[str], text: str) -> Entity | None:
    canonical = canonical_entity_name(text)
    query = db.query(Entity).filter(Entity.deleted_at.is_(None))
    if knowledge_base_ids:
        query = query.filter(Entity.knowledge_base_id.in_(knowledge_base_ids))
    exact = query.filter(Entity.canonical_name == canonical).first()
    return exact or query.filter(Entity.name.ilike(f"%{text}%")).first()


def relationship_sources(db: Session, relationships: list[Relationship]) -> list[dict]:
    ids = [relationship.id for relationship in relationships]
    if not ids:
        return []
    rows = db.query(RelationshipSource, Document).join(Document, Document.id == RelationshipSource.document_id).filter(RelationshipSource.relationship_id.in_(ids)).all()
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


def analyze_impact(db: Session, subject: str, knowledge_base_ids: list[str], max_depth: int, include_indirect: bool) -> dict:
    entity = resolve_entity(db, knowledge_base_ids, subject)
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


def create_document_job(db: Session, knowledge_base_id: str, upload, title: str | None = None, document_type: str = "general") -> tuple[Document, ProcessingJob]:
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
                   document_type=document_type, status="queued")
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


def replace_document_chunks(db: Session, document: Document, text: str) -> None:
    settings = get_settings()
    db.query(DocumentChunk).filter_by(document_id=document.id).delete(synchronize_session=False)
    for index, (char_start, char_end, content) in enumerate(
        split_text(text, settings.default_chunk_size, settings.default_chunk_overlap)
    ):
        db.add(DocumentChunk(
            document_id=document.id,
            knowledge_base_id=document.knowledge_base_id,
            chunk_index=index,
            content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            char_start=char_start,
            char_end=char_end,
            token_count=len(re.findall(r"\S+", content)),
        ))
    db.flush()


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
            doc.legal_metadata = OpenRouterClient().extract_legal_metadata(doc.title or doc.original_filename, text)
            sync_legal_document_graph(db, doc)
            job.status, job.current_stage, job.progress_percent = "completed", "completed", 100
            db.commit()
            return True
        job.current_stage, job.progress_percent = "chunking", 45
        if reindex_only and db.query(DocumentChunk.id).filter_by(document_id=doc.id).first():
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


def build_retrieval_plan(db: Session, query: str, kb_ids: list[str], max_sources: int) -> PlannerDecision:
    """Build a policy-constrained plan; invoke the LLM only when rules are ambiguous."""
    rows = db.query(KnowledgeBase).filter(KnowledgeBase.id.in_(kb_ids)).all() if kb_ids else []
    policy = intersect_policies([policy_from_config(row.retrieval_config) for row in rows])
    decision = rule_plan(query, policy, max_sources)
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
                    trace: list[dict] | None = None, plan: RetrievalPlan | None = None) -> RetrievalEvidence:
    plan = plan or rule_plan(query, policy_from_config(None), limit).plan
    channels: list[RetrievalEvidence] = []
    db_channels = {
        RetrievalChannel.VECTOR: lambda session: query_database_vectors(session, query, kb_ids, limit, trace),
        RetrievalChannel.FULLTEXT: lambda session: query_database_chunks(session, query, kb_ids, limit, trace),
        RetrievalChannel.GRAPH: lambda session: query_database_graph(session, query, kb_ids, limit, trace, plan.graph_depth),
    }
    for channel in (RetrievalChannel.VECTOR, RetrievalChannel.FULLTEXT, RetrievalChannel.GRAPH):
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
    channels = [channel_results[channel] for channel in (RetrievalChannel.VECTOR, RetrievalChannel.FULLTEXT, RetrievalChannel.GRAPH, RetrievalChannel.LIGHTRAG) if channel in channel_results]
    evidence = rerank_evidence(query, fuse_evidence(*channels, limit=limit), limit, trace)
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


def query_database_vectors(db: Session, query: str, kb_ids: list[str], limit: int,
                           trace: list[dict] | None = None) -> RetrievalEvidence:
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
    records = rows.order_by(distance).limit(limit).all()
    sources = [{"citation_id": f"S{i}", "document_id": document.id, "title": document.title,
                "chunk_id": chunk.id, "excerpt": chunk.content[:500], "relevance": max(0.0, 1.0 - float(item_distance))}
               for i, (chunk, document, item_distance) in enumerate(records, 1)]
    _append_retrieval_trace(trace, channel="semantic_vector", system="OpenRouter embeddings → PostgreSQL + pgvector",
                            status="used", started_at=started_at, result_count=len(sources), detail="cosine similarity")
    return RetrievalEvidence(sources, [], [], [])


def query_database_chunks(db: Session, query: str, kb_ids: list[str], limit: int,
                          trace: list[dict] | None = None) -> RetrievalEvidence:
    started_at = time.monotonic()
    words = [word for word in re.findall(r"[\w-]+", query.lower()) if len(word) > 1]
    rows = db.query(DocumentChunk, Document).join(Document, Document.id == DocumentChunk.document_id).filter(
        Document.status == "completed", Document.deleted_at.is_(None)
    )
    if kb_ids:
        rows = rows.filter(DocumentChunk.knowledge_base_id.in_(kb_ids))
    if words:
        if db.get_bind().dialect.name == "postgresql":
            vector = func.to_tsvector("simple", DocumentChunk.content)
            tsquery = func.websearch_to_tsquery("simple", query)
            rows = rows.filter(vector.op("@@")(tsquery)).order_by(func.ts_rank_cd(vector, tsquery).desc())
        else:
            rows = rows.filter(or_(*[DocumentChunk.content.ilike(f"%{word}%") for word in words[:8]]))
    records = rows.limit(limit).all()
    sources = [{"citation_id": f"S{i}", "document_id": document.id, "title": document.title,
                "chunk_id": chunk.id, "excerpt": chunk.content[:500], "relevance": 1.0 / i}
               for i, (chunk, document) in enumerate(records, 1)]
    system = "PostgreSQL full-text search" if db.get_bind().dialect.name == "postgresql" else "SQL text fallback"
    _append_retrieval_trace(trace, channel="full_text", system=system, status="used",
                            started_at=started_at, result_count=len(sources), detail="keyword retrieval")
    return RetrievalEvidence(sources, [], [], [])


def query_database_graph(db: Session, query: str, kb_ids: list[str], limit: int,
                         trace: list[dict] | None = None, depth: int = 1) -> RetrievalEvidence:
    """Use Neo4j as a bounded ID accelerator and PostgreSQL for evidence."""
    started_at = time.monotonic()
    entity = resolve_entity(db, kb_ids, query)
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
            result = store.traverse(entity.id, entity.knowledge_base_id, depth, max(limit * 10, 50))
            relationship_ids = result.get("relationship_ids", [])
            node_ids = result.get("node_ids", [])
            if relationship_ids:
                accelerator = "neo4j accelerator → PostgreSQL evidence"
        except (httpx.HTTPError, RuntimeError, ValueError):
            relationship_ids = []
    if relationship_ids:
        relationships = db.query(Relationship).filter(
            Relationship.id.in_(relationship_ids), Relationship.knowledge_base_id == entity.knowledge_base_id,
            Relationship.deleted_at.is_(None),
            (Relationship.is_legal.is_(False)) | (Relationship.review_status == "verified"),
        ).all()
        node_rows = db.query(Entity).filter(Entity.id.in_(node_ids), Entity.knowledge_base_id == entity.knowledge_base_id,
                                            Entity.deleted_at.is_(None)).all()
        graph = {"nodes": [{"id": row.id, "name": row.name, "type": row.entity_type} for row in node_rows],
                 "edges": [{"id": row.id, "source": row.source_entity_id, "target": row.target_entity_id, "type": row.relationship_type} for row in relationships]}
    else:
        graph = entity_graph(db, entity, min(depth, 3))
        relationships = db.query(Relationship).filter(Relationship.id.in_([edge["id"] for edge in graph["edges"]])).all()
    sources = relationship_sources(db, relationships)[:limit]
    for source in sources:
        source.pop("_relationship_id", None)
    _append_retrieval_trace(trace, channel="graph", system=accelerator, status="used",
                            started_at=started_at, result_count=len(sources), detail="verified/manual bounded traversal")
    return RetrievalEvidence(sources, graph["nodes"], graph["edges"], graph["edges"])


def fuse_evidence(*channels: RetrievalEvidence, limit: int) -> RetrievalEvidence:
    """Use reciprocal-rank fusion across semantic, FTS, and graph channels."""
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
    ordered = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)[:limit]
    sources = [{**item["source"], "citation_id": f"S{index}", "relevance": round(item["score"], 6)} for index, item in enumerate(ordered, 1)]
    answer = next((channel.answer for channel in channels if channel.answer), None)
    entities = [item for channel in channels for item in channel.entities]
    relationships = [item for channel in channels for item in channel.relationships]
    paths = [item for channel in channels for item in channel.paths]
    return RetrievalEvidence(sources, entities, relationships, paths, answer)


def rerank_evidence(query: str, evidence: RetrievalEvidence, limit: int,
                    trace: list[dict] | None = None) -> RetrievalEvidence:
    """Optionally apply a cross-encoder reranker after deterministic fusion."""
    client = OpenRouterClient()
    if not client.reranker_enabled or not evidence.sources:
        _append_retrieval_trace(trace, channel="rerank", system="OpenRouter reranker", status="skipped", started_at=time.monotonic(),
                                detail="not configured" if not client.reranker_enabled else "no candidates")
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


def compose_cited_answer(evidence: RetrievalEvidence) -> str:
    if not evidence.sources:
        return "Insufficient evidence was found in the selected knowledge bases."
    citations = " ".join(f"[{source['citation_id']}]" for source in evidence.sources)
    answer = (evidence.answer or "").strip()
    if not answer:
        return f"Supporting knowledge was found: {citations}"
    if not re.search(r"\[S\d+\]", answer):
        return f"{answer}\n\nSources: {citations}"
    return answer


def build_query_result(db: Session, query: str, kb_ids: list[str], max_sources: int, token_id: str | None = None) -> dict:
    retrieval_trace: list[dict] = []
    decision = build_retrieval_plan(db, query, kb_ids, max_sources)
    evidence = query_documents(db, query, kb_ids, decision.plan.max_sources, retrieval_trace, decision.plan)
    intent = decision.plan.intent
    answer = compose_cited_answer(evidence)
    metrics.observe_query_outcome("insufficient_evidence" if not evidence.sources else "evidence_found")
    result = {"status": "success", "result_id": "", "answer": answer,
              "insufficient_evidence": not bool(evidence.sources), "entities": evidence.entities,
              "relationships": evidence.relationships, "paths": evidence.paths, "sources": evidence.sources,
              "warnings": [], "metadata": {"knowledge_base_ids": kb_ids, "retrieval_strategy": intent,
                                                "retrieval_plan": decision.plan.model_dump(mode="json"),
                                                "retrieval_trace": retrieval_trace}}
    saved = QueryResult(token_key_id=token_id, result_json=result, expires_at=datetime.utcnow() + timedelta(minutes=30))
    db.add(saved); db.flush(); result["result_id"] = saved.id; saved.result_json = result; db.commit()
    return result
