"""Evidence-backed custom metadata. Candidates never masquerade as manual facts."""
import hashlib
import json
from datetime import datetime, timedelta

from .document_templates import metadata_search_text, validate_metadata_values
from .models import Document, ProcessingJob
from .openrouter import OpenRouterClient

JOB_TYPE = "EXTRACT_DOCUMENT_METADATA"
EXTRACTOR_VERSION = "1"
WINDOW = 12000
MAX_WINDOWS = 4


def extraction_fields(document):
    observations = document.metadata_observations or {}
    return [f for f in (document.metadata_template_fields or [])
            if f.get("fill_mode") == "extract"
            and not observations.get(f["key"], {}).get("locked")
            and f["key"] not in (document.document_metadata or {})]


def update_status(document):
    observations = document.metadata_observations or {}
    pending = [f for f in document.metadata_template_fields or [] if f.get("fill_mode") == "extract"
               and f["key"] not in (document.document_metadata or {})
               and observations.get(f["key"], {}).get("status") != "not_found_confirmed"]
    document.metadata_status = "needs_review" if pending else "complete"


def queue_metadata_extraction(db, document):
    # Callers hold a document row lock (or own an uncommitted new document).
    digest = hashlib.sha256((document.extracted_text or "").encode()).hexdigest()
    observations = dict(document.metadata_observations or {})
    stale = {key for key, item in observations.items() if item.get("origin") == "document_extraction"
             and not item.get("locked") and item.get("content_version") != digest}
    if stale:
        from .services import sync_document_metadata_graph, sync_document_metadata_values
        document.document_metadata = {k: v for k, v in (document.document_metadata or {}).items() if k not in stale}
        document.metadata_observations = {k: v for k, v in observations.items() if k not in stale}
        document.metadata_revision += 1
        document.metadata_search_text = metadata_search_text(document.metadata_template_fields, document.document_metadata)
        sync_document_metadata_values(db, document)
        sync_document_metadata_graph(db, document)
    if not extraction_fields(document):
        return False
    active = db.query(ProcessingJob.id).filter(
        ProcessingJob.document_id == document.id, ProcessingJob.job_type == JOB_TYPE,
        ProcessingJob.status.in_(["queued", "running"]),
    ).first()
    if active:
        return False
    document.metadata_status = "queued"
    db.add(ProcessingJob(document_id=document.id, knowledge_base_id=document.knowledge_base_id, job_type=JOB_TYPE))
    db.flush()
    return True


def extract_candidates(fields, text, client):
    """Bound provider work and verify source quotes against the exact input version."""
    collected = {f["key"]: [] for f in fields}
    invalid = set()
    truncated = len(text) > WINDOW * MAX_WINDOWS
    for start in range(0, min(len(text), WINDOW * MAX_WINDOWS), WINDOW):
        window = text[start:start + WINDOW]
        result = client.extract_document_metadata(fields, window)
        for field in fields:
            key = field["key"]
            candidates = result.get(key, [])
            if not isinstance(candidates, list) or len(candidates) > 20:
                invalid.add(key)
                continue
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    invalid.add(key)
                    continue
                quote = candidate.get("evidence_quote")
                value = candidate.get("value")
                if not isinstance(quote, str) or not quote.strip() or len(quote) > 4000 or quote not in window:
                    invalid.add(key)
                    continue
                try:
                    value = validate_metadata_values([{**field, "required": False}], {key: value})[key]
                    # JSON rejects NaN/Infinity even though Python floats accept them.
                    json.dumps(value, allow_nan=False)
                except (ValueError, KeyError, TypeError):
                    invalid.add(key)
                    continue
                offset = start + window.index(quote)
                entry = {"value": value, "evidence": {"quote": quote, "char_start": offset, "char_end": offset + len(quote)}}
                if not any(c["value"] == value for c in collected[key]):
                    if len(collected[key]) < 20:
                        collected[key].append(entry)
                    else:
                        invalid.add(key)
    observations = {}
    digest = hashlib.sha256(text.encode()).hexdigest()
    for field in fields:
        key = field["key"]
        candidates = collected[key]
        status = "suggested" if candidates else "not_found"
        if len(candidates) > 1:
            status = "conflict"
        elif key in invalid:
            status = "invalid_evidence"
        elif truncated:
            status = "partial"
        elif len(candidates) == 1:
            value, evidence = candidates[0]["value"], candidates[0]["evidence"]
            # Only literal text/enum values can pass the initial automatic policy.
            # Dates, numbers, booleans and graph facts require human verification.
            if (field.get("review_policy", "evidence") == "evidence"
                    and field.get("field_type", "text") in {"text", "textarea", "select"}
                    and not field.get("graph_relationship")
                    and isinstance(value, str) and value in evidence["quote"]):
                status = "auto_accepted"
        observations[key] = {"status": status, "origin": "document_extraction", "candidates": candidates,
                             "content_version": digest, "extractor_version": EXTRACTOR_VERSION,
                             "coverage": "partial" if truncated else "full", "locked": False}
    return observations


def process_metadata_job(db, job):
    """Independent retry and compare-and-set publication after slow provider work."""
    from .services import sync_document_metadata_graph, sync_document_metadata_values

    claimed = db.query(ProcessingJob).filter_by(id=job.id, status="queued").update({"status": "running"}, synchronize_session=False)
    if not claimed:
        db.rollback()
        return
    db.refresh(job)
    document = db.query(Document).filter_by(id=job.document_id).with_for_update().one()
    if document.deleted_at:
        job.status = "cancelled"
        db.commit()
        return
    fields = extraction_fields(document)
    text = document.extracted_text or ""
    revision = document.metadata_revision
    template_version = document.metadata_template_version
    job.status, job.current_stage = "running", "metadata_extraction"
    job.attempt_count += 1
    document.metadata_status = "running"
    db.commit()
    try:
        if not text:
            raise RuntimeError("METADATA_TEXT_NOT_READY")
        observations = extract_candidates(fields, text, OpenRouterClient()) if fields else {}
        document = db.query(Document).filter_by(id=job.document_id).populate_existing().with_for_update().one()
        if document.deleted_at:
            job.status = "cancelled"
            db.commit()
            return
        if document.metadata_revision != revision or document.extracted_text != text or document.metadata_template_version != template_version:
            # Never publish an extraction against a stale source or manual edit.
            job.status, job.current_stage = "queued", "superseded_retry"
            document.metadata_status = "queued"
            db.commit()
            return
        values = dict(document.document_metadata or {})
        for key, observation in observations.items():
            observation["template_version"] = template_version
            if observation["status"] == "auto_accepted":
                values[key] = observation["candidates"][0]["value"]
        document.document_metadata = values
        document.metadata_observations = {**(document.metadata_observations or {}), **observations}
        document.metadata_revision += 1
        document.metadata_search_text = metadata_search_text(document.metadata_template_fields, values)
        update_status(document)
        sync_document_metadata_values(db, document)
        sync_document_metadata_graph(db, document)
        job.status, job.current_stage, job.progress_percent = "completed", "completed", 100
        job.error_code, job.error_message = None, None
        db.commit()
    except Exception as exc:
        db.rollback()
        document = db.query(Document).filter_by(id=job.document_id).populate_existing().with_for_update().one()
        if document.deleted_at:
            job.status = "cancelled"
        else:
            retry = str(exc) == "OPENROUTER_UNAVAILABLE" and job.attempt_count < 3
            job.status = "queued" if retry else "failed"
            job.current_stage = "retry_wait" if retry else "failed"
            job.next_attempt_at = datetime.utcnow() + timedelta(seconds=2 ** job.attempt_count)
            job.error_code = str(exc) if str(exc) in {"OPENROUTER_UNAVAILABLE", "OPENROUTER_API_KEY_NOT_CONFIGURED", "METADATA_TEXT_NOT_READY", "METADATA_EXTRACTION_INVALID_RESPONSE"} else "METADATA_EXTRACTION_FAILED"
            job.error_message = "Metadata extraction did not complete; document search remains available."
            if document.metadata_revision != revision:
                update_status(document)
            else:
                document.metadata_status = "queued" if retry else "failed"
        db.commit()
