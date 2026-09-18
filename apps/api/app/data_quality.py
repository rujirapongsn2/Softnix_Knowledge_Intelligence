"""Deterministic document quality signals for AI retrieval and citations."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Document, DocumentChunk, Entity, EntitySource, LegalInstrument


LEGAL_TYPES = {"legal", "regulation", "contract"}
WEIGHTS = {"content": 20, "structure": 15, "metadata": 20, "retrieval": 20, "citation": 20, "graph": 5}
OCR_PAGE_RE = re.compile(r"<!--\s*OCR:\s*page\s+(\d+)\s*-->", re.IGNORECASE)
THAI_RE = re.compile(r"[\u0E00-\u0E7F]")
CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")


def _clamp(value: float) -> int:
    return max(0, min(100, round(value)))


def _file_available(document: Document) -> bool:
    if not document.storage_path:
        return False
    try:
        path = Path(document.storage_path).resolve()
        path.relative_to(get_settings().file_root.resolve())
    except (OSError, ValueError):
        return False
    return path.is_file()


def _text_anomalies(text: str) -> tuple[int, int, bool]:
    replacement_count = text.count("�")
    control_count = sum(1 for char in text if ord(char) < 32 and char not in "\n\r\t")
    mixed_script = len(THAI_RE.findall(text)) >= 100 and len(CYRILLIC_RE.findall(text)) >= 2
    return replacement_count, control_count, mixed_script


def _page_quality(text: str) -> list[dict[str, Any]]:
    matches = list(OCR_PAGE_RE.finditer(text))
    pages = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        page_text = text[match.end():end].strip()
        replacements, controls, mixed_script = _text_anomalies(page_text)
        anomalies = replacements + controls + (1 if mixed_script else 0)
        status = "empty" if len(page_text) < 40 else "warning" if anomalies else "good"
        pages.append({"page": int(match.group(1)), "status": status, "characters": len(page_text), "anomalies": anomalies})
    return pages


def _normal(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name")
    return " ".join(str(value or "").casefold().split())


def _metadata_consistency(document: Document) -> list[str]:
    custom = document.document_metadata or {}
    legal = document.legal_metadata or {}
    instrument = legal.get("instrument") if isinstance(legal.get("instrument"), dict) else {}
    conflicts = []
    issuer_a = _normal(custom.get("issuer"))
    issuer_b = _normal(instrument.get("issuer"))
    if issuer_a and issuer_b and issuer_a != issuer_b:
        conflicts.append("issuer")
    date_a = _normal(custom.get("effective_date"))
    date_b = _normal(legal.get("effective_date") or instrument.get("effective_date"))
    if date_a and date_b and date_a != date_b:
        conflicts.append("effective_date")
    return conflicts


def build_document_quality_report(db: Session, document: Document) -> dict[str, Any]:
    """Return explainable readiness dimensions without an LLM quality judge."""
    text = document.extracted_text or ""
    is_legal = document.document_type in LEGAL_TYPES
    source_available = _file_available(document)
    chunks = db.query(
        func.count(DocumentChunk.id),
        func.count(DocumentChunk.embedding),
        func.sum(case((DocumentChunk.section_label.is_not(None), 1), else_=0)),
        func.sum(case((DocumentChunk.content != "", 1), else_=0)),
    ).filter(DocumentChunk.document_id == document.id).one()
    chunk_count = int(chunks[0] or 0)
    embedding_count = int(chunks[1] or 0)
    section_count = int(chunks[2] or 0)
    content_chunk_count = int(chunks[3] or 0)
    embedding_coverage = embedding_count / chunk_count if chunk_count else 0.0
    section_coverage = section_count / chunk_count if chunk_count else 0.0

    pages = _page_quality(text)
    page_count = len(pages)
    empty_pages = sum(1 for page in pages if page["status"] == "empty")
    replacements, controls, mixed_script = _text_anomalies(text)
    anomaly_count = replacements + controls + (1 if mixed_script else 0)

    content_score = 0
    if text.strip():
        content_score += 45
        content_score += 20 if len(text.strip()) >= 200 else 8
        content_score += 15 if not empty_pages else max(0, 15 - empty_pages * 5)
        content_score += 20 if not anomaly_count else max(0, 20 - min(20, anomaly_count * 4))

    structure_score = 0
    if chunk_count:
        structure_score += 45
        structure_score += 20 * (content_chunk_count / chunk_count)
        structure_score += 35 * section_coverage if is_legal else 35

    fields = [field for field in (document.metadata_template_fields or []) if isinstance(field, dict) and field.get("key")]
    values = document.document_metadata or {}
    observations = document.metadata_observations or {}
    required = [field for field in fields if field.get("required")]
    populated = [field for field in fields if values.get(field["key"]) not in (None, "")]
    required_populated = [field for field in required if values.get(field["key"]) not in (None, "")]
    conflicts = [key for key, observation in observations.items() if isinstance(observation, dict) and observation.get("status") in {"conflict", "invalid_evidence"}]
    consistency_conflicts = _metadata_consistency(document)
    verified = [key for key, observation in observations.items() if isinstance(observation, dict) and observation.get("status") in {"human_verified", "not_found_confirmed"}]
    custom_coverage = len(populated) / len(fields) if fields else 1.0
    required_coverage = len(required_populated) / len(required) if required else 1.0
    metadata_score = 35 * custom_coverage + 35 * required_coverage
    if document.metadata_status in {"complete", "not_started"}:
        metadata_score += 15
    elif document.metadata_status == "needs_review":
        metadata_score += 5
    if is_legal:
        metadata_score += 15 if document.legal_metadata else 0
    else:
        metadata_score += 15
    metadata_score -= min(30, 10 * (len(conflicts) + len(consistency_conflicts)))

    retrieval_score = 0
    retrieval_score += 25 if document.status == "completed" else 0
    retrieval_score += 15 if document.indexed_at else 0
    retrieval_score += 20 if chunk_count else 0
    retrieval_score += 40 * embedding_coverage

    citation_score = 25 if source_available else 0
    if chunk_count:
        citation_score += 25
        citation_score += 25 * (content_chunk_count / chunk_count)
        citation_score += 25 * section_coverage if is_legal else 25

    instrument = db.query(LegalInstrument).filter_by(document_id=document.id).first() if is_legal else None
    legal_sources = 0
    if is_legal:
        legal_sources = db.query(func.count(EntitySource.id)).join(Entity, Entity.id == EntitySource.entity_id).filter(
            EntitySource.document_id == document.id, Entity.is_legal.is_(True), Entity.deleted_at.is_(None),
        ).scalar() or 0
    graph_score = 100 if not is_legal else (100 if instrument and legal_sources else 55 if instrument else 0)

    dimensions = {
        "content": _clamp(content_score),
        "structure": _clamp(structure_score),
        "metadata": _clamp(metadata_score),
        "retrieval": _clamp(retrieval_score),
        "citation": _clamp(citation_score),
        "graph": _clamp(graph_score),
    }
    score = round(sum(dimensions[key] * weight for key, weight in WEIGHTS.items()) / 100)

    blockers: list[str] = []
    warnings: list[str] = []
    if document.status != "completed": blockers.append("processing_incomplete")
    if not source_available: blockers.append("source_file_missing")
    if not text.strip(): blockers.append("text_missing")
    if not chunk_count: blockers.append("chunks_missing")
    if chunk_count and not embedding_count: blockers.append("embeddings_missing")
    if 0 < embedding_count < chunk_count: warnings.append("embedding_coverage_partial")
    if anomaly_count: warnings.append("ocr_anomaly_detected")
    if empty_pages: warnings.append("empty_ocr_pages")
    if required_coverage < 1: warnings.append("required_metadata_missing")
    elif custom_coverage < 1: warnings.append("metadata_incomplete")
    if document.metadata_status == "needs_review": warnings.append("metadata_needs_review")
    if conflicts: warnings.append("metadata_evidence_conflict")
    if consistency_conflicts: warnings.append("metadata_consistency_conflict")
    if is_legal and not document.legal_metadata: warnings.append("legal_metadata_missing")
    if is_legal and chunk_count and section_coverage < 0.5: warnings.append("citation_locator_coverage_low")
    if is_legal and not instrument: warnings.append("legal_graph_missing")

    reviewed_keys = set(verified)
    all_reviewed = bool(fields) and all(field["key"] in reviewed_keys for field in fields)
    if is_legal:
        all_reviewed = all_reviewed and bool(instrument and instrument.review_status == "verified")
    if blockers:
        status = "not_queryable"
    elif score >= 90 and not warnings and all_reviewed:
        status = "verified"
    elif score >= 80:
        status = "ai_ready"
    else:
        status = "needs_review"

    return {
        "schema_version": "ski.quality.v1",
        "status": status,
        "score": score,
        "blockers": blockers,
        "warnings": list(dict.fromkeys(warnings)),
        "dimensions": dimensions,
        "metrics": {
            "source_available": source_available,
            "text_characters": len(text),
            "ocr_pages": page_count,
            "empty_ocr_pages": empty_pages,
            "pages": pages,
            "text_anomalies": anomaly_count,
            "chunks": chunk_count,
            "embedded_chunks": embedding_count,
            "embedding_coverage": round(embedding_coverage, 4),
            "section_locator_coverage": round(section_coverage, 4),
            "metadata_fields": len(fields),
            "metadata_populated": len(populated),
            "metadata_conflicts": sorted(set(conflicts + consistency_conflicts)),
            "legal_graph_sources": int(legal_sources),
        },
        "content_revision": document.checksum_sha256,
        "metadata_revision": document.metadata_revision,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
