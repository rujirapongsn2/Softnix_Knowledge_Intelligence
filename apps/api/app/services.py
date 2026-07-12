import hashlib
import logging
import mimetypes
import re
import time
import uuid
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
from .external_ocr import ExternalOcrClient
from .graph_store import Neo4jGraphStore
from .models import Document, DocumentChunk, Entity, EntitySource, GraphProjectionEvent, ProcessingJob, QueryResult, Relationship, RelationshipSource
from .openrouter import OpenRouterClient
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
    "retrieval_mode": "auto", "enable_vector": True, "enable_graph": True,
    "enable_fulltext": True, "enable_reranker": True, "default_top_k": 12,
    "maximum_top_k": 30, "maximum_graph_depth": 3, "citation_required": True,
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


def create_entity(db: Session, knowledge_base_id: str, payload) -> Entity:
    canonical = canonical_entity_name(payload.name)
    entity = db.query(Entity).filter_by(knowledge_base_id=knowledge_base_id, canonical_name=canonical, entity_type=payload.entity_type).first()
    if not entity:
        entity = Entity(knowledge_base_id=knowledge_base_id, name=payload.name, canonical_name=canonical,
                        entity_type=payload.entity_type, description=payload.description, aliases=payload.aliases,
                        attributes=payload.attributes, confidence=payload.confidence)
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
                                    relationship_type=payload.relationship_type, description=payload.description, confidence=payload.confidence)
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
        entity = db.query(Entity).filter_by(knowledge_base_id=document.knowledge_base_id, canonical_name=canonical, entity_type=entity_type).first()
        if not entity:
            entity = Entity(knowledge_base_id=document.knowledge_base_id, name=name, canonical_name=canonical, entity_type=entity_type,
                            description=str(props.get("description") or "")[:5000] or None, confidence=1.0)
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
                                        relationship_type=relationship_type, description=description, confidence=1.0)
            db.add(relationship); db.flush(); relationship_count += 1
            db.add(GraphProjectionEvent(event_type="relationship", relationship_id=relationship.id))
        elif relationship.deleted_at:
            relationship.deleted_at = None
        excerpt = description or f"{source.name} relates to {target.name}"
        if not db.query(RelationshipSource).filter_by(relationship_id=relationship.id, document_id=document.id, excerpt=excerpt).first():
            db.add(RelationshipSource(relationship_id=relationship.id, document_id=document.id, excerpt=excerpt)); relationship.source_count += 1
    db.commit()
    return {"entities": entity_count, "relationships": relationship_count}


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


def query_documents(db: Session, query: str, kb_ids: list[str], limit: int) -> RetrievalEvidence:
    channels = [query_database_vectors(db, query, kb_ids, limit), query_database_chunks(db, query, kb_ids, limit)]
    engine = LightRAGRetrievalEngine()
    if engine.enabled and len(kb_ids) == 1:
        try:
            channels.append(engine.query(query, kb_ids, limit))
        except RuntimeError:
            pass
    evidence = rerank_evidence(query, fuse_evidence(*channels, limit=limit), limit)
    if evidence.sources:
        try:
            evidence.answer = OpenRouterClient().answer_from_sources(query, evidence.sources)
        except RuntimeError:
            evidence.answer = None
    return evidence


def query_database_vectors(db: Session, query: str, kb_ids: list[str], limit: int) -> RetrievalEvidence:
    if db.get_bind().dialect.name != "postgresql":
        return RetrievalEvidence([], [], [], [])
    client = OpenRouterClient()
    if not client.embeddings_enabled:
        return RetrievalEvidence([], [], [], [])
    try:
        query_vector = client.embed_texts([query])[0]
    except RuntimeError:
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
    return RetrievalEvidence(sources, [], [], [])


def query_database_chunks(db: Session, query: str, kb_ids: list[str], limit: int) -> RetrievalEvidence:
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
    return RetrievalEvidence(sources, [], [], [])


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


def rerank_evidence(query: str, evidence: RetrievalEvidence, limit: int) -> RetrievalEvidence:
    """Optionally apply a cross-encoder reranker after deterministic fusion."""
    client = OpenRouterClient()
    if not client.reranker_enabled or not evidence.sources:
        return evidence
    candidates = evidence.sources[:get_settings().rerank_candidate_limit]
    try:
        ranked = client.rerank(query, [item["excerpt"] for item in candidates], limit)
    except RuntimeError:
        return evidence
    ordered = []
    for index, relevance in ranked:
        if 0 <= index < len(candidates):
            ordered.append({**candidates[index], "relevance": relevance})
    if not ordered:
        return evidence
    sources = [{**source, "citation_id": f"S{index}"} for index, source in enumerate(ordered[:limit], 1)]
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
    evidence = query_documents(db, query, kb_ids, max_sources)
    intent = plan_intent(query)
    answer = compose_cited_answer(evidence)
    result = {"status": "success", "result_id": "", "answer": answer,
              "insufficient_evidence": not bool(evidence.sources), "entities": evidence.entities,
              "relationships": evidence.relationships, "paths": evidence.paths, "sources": evidence.sources,
              "warnings": [], "metadata": {"knowledge_base_ids": kb_ids, "retrieval_strategy": intent}}
    saved = QueryResult(token_key_id=token_id, result_json=result, expires_at=datetime.utcnow() + timedelta(minutes=30))
    db.add(saved); db.flush(); result["result_id"] = saved.id; saved.result_json = result; db.commit()
    return result
