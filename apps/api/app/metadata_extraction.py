"""Evidence-backed custom metadata. Candidates never masquerade as manual facts."""
import hashlib
import json
import re
from datetime import datetime, timedelta

from .document_templates import TEXT_LIST_MAX_ITEMS, metadata_search_text, validate_metadata_values
from .models import Document, JobType, ProcessingJob
from .openrouter import OpenRouterClient

EXTRACTOR_VERSION = "1"
WINDOW = 12000
WINDOW_OVERLAP = 1000
# 240,000 characters. A longer document is read up to here and its fields are marked partial.
MAX_WINDOWS = 20


def extraction_fields(document):
    observations = document.metadata_observations or {}
    digest = hashlib.sha256((document.extracted_text or "").encode()).hexdigest()
    return [f for f in (document.metadata_template_fields or [])
            if f.get("fill_mode") == "extract"
            and not observations.get(f["key"], {}).get("locked")
            # A value from an earlier source version must be refreshed, but it
            # remains visible until a new, evidence-backed result is ready.
            and (f["key"] not in (document.document_metadata or {})
                 or (observations.get(f["key"], {}).get("origin") == "document_extraction"
                     and observations.get(f["key"], {}).get("content_version") != digest))]


def pending_review_keys(fields, values, observations):
    """Keys of AI-extracted fields that still wait for a person: empty, or filled by extraction without auto-acceptance."""
    return [f["key"] for f in fields or []
            if f.get("fill_mode") == "extract"
            and not observations.get(f["key"], {}).get("locked")
            and observations.get(f["key"], {}).get("status") != "not_found_confirmed"
            and (f["key"] not in values
                 or (observations.get(f["key"], {}).get("origin") == "document_extraction"
                     and observations.get(f["key"], {}).get("status") != "auto_accepted"))]


def update_status(document):
    pending = pending_review_keys(document.metadata_template_fields, document.document_metadata or {}, document.metadata_observations or {})
    document.metadata_status = "needs_review" if pending else "complete"


def queue_metadata_extraction(db, document):
    # Callers hold a document row lock (or own an uncommitted new document).
    # Do not delete the last evidence-backed value just because the source has
    # changed.  The old implementation cleared it before the provider call;
    # a timeout or invalid response therefore made a visible, searchable value
    # disappear.  ``extraction_fields`` above includes stale values in the next
    # run and publication replaces them only after that run completes.
    if not extraction_fields(document):
        return False
    active = db.query(ProcessingJob.id).filter(
        ProcessingJob.document_id == document.id, ProcessingJob.job_type == JobType.EXTRACT_DOCUMENT_METADATA,
        ProcessingJob.status.in_(["queued", "running"]),
    ).first()
    if active:
        return False
    document.metadata_status = "queued"
    db.add(ProcessingJob(document_id=document.id, knowledge_base_id=document.knowledge_base_id, job_type=JobType.EXTRACT_DOCUMENT_METADATA))
    db.flush()
    return True


_WRAPPING_MARKS = "\"'\u201c\u201d\u2018\u2019\u00ab\u00bb"


def _locate_quote(quote, window):
    """The exact text in the window that a model's quote points at, or None. Models often wrap a copied quote in quotation
    marks or change its line breaks and spacing, so those differences are ignored. The text returned is always the document's own."""
    if quote in window:
        return quote
    cleaned = quote.strip().strip(_WRAPPING_MARKS).strip()
    if not cleaned:
        return None
    if cleaned in window:
        return cleaned
    match = re.search(r"\s+".join(re.escape(part) for part in cleaned.split()), window)
    return match.group(0) if match else None


def _read_answer(field, answer, window):
    """Valid candidates (value plus verbatim quote) from one field's answer, and whether any of it was unusable."""
    if not isinstance(answer, list) or len(answer) > 20:
        return [], True
    entries, invalid = [], False
    for candidate in answer:
        quote = candidate.get("evidence_quote") if isinstance(candidate, dict) else None
        quote = _locate_quote(quote, window) if isinstance(quote, str) and len(quote) <= 4000 else None
        if not quote:
            invalid = True
            continue
        try:
            # A list field's answer is one item per object; the item is validated as a one-item list.
            is_list = field.get("field_type") == "text_list"
            value = validate_metadata_values([{**field, "required": False}], {field["key"]: [candidate.get("value")] if is_list else candidate.get("value")})[field["key"]]
            value = value[0] if is_list else value
            # JSON rejects NaN/Infinity even though Python floats accept them.
            json.dumps(value, allow_nan=False)
        except (ValueError, KeyError, TypeError):
            invalid = True
            continue
        entries.append((value, quote))
    return entries, invalid


def _read_window(fields, window, client):
    """Answers for one window. A field whose answer was unusable is asked once more on its own, because models
    sometimes drop the quote or the list shape the first time. The retry replaces the first answer unless it is worse."""
    result = client.extract_document_metadata(fields, window)
    answers = {f["key"]: _read_answer(f, result.get(f["key"], []), window) for f in fields}
    retry = [f for f in fields if answers[f["key"]][1]]
    if retry:
        again = client.extract_document_metadata(retry, window)
        for field in retry:
            second = _read_answer(field, again.get(field["key"], []), window)
            if not second[1] or len(second[0]) >= len(answers[field["key"]][0]):
                answers[field["key"]] = second
    return answers


def _evidence(window, start, quote):
    offset = start + window.index(quote)
    return {"quote": quote, "char_start": offset, "char_end": offset + len(quote)}


def _auto_acceptable(field, candidate):
    """Only values that are literally their own quote pass without a person. Dates, numbers, booleans and graph facts need review."""
    if field.get("review_policy", "evidence") != "evidence" or field.get("graph_relationship"):
        return False
    kind, value = field.get("field_type", "text"), candidate["value"]
    if kind in {"text", "textarea", "select"}:
        return isinstance(value, str) and value in candidate["evidence"]["quote"]
    if kind == "text_list":
        return all(item in evidence["quote"] for item, evidence in zip(value, candidate["item_evidence"]))
    return False


def extract_candidates(fields, text, client):
    """Bound provider work and verify source quotes against the exact input version."""
    collected = {f["key"]: [] for f in fields}
    invalid = set()
    truncated = len(text) > WINDOW * MAX_WINDOWS
    for start in range(0, min(len(text), WINDOW * MAX_WINDOWS), WINDOW):
        # The overlap keeps a sentence that crosses a window boundary whole in one of the two windows.
        window = text[start:start + WINDOW + WINDOW_OVERLAP]
        for key, (entries, bad) in _read_window(fields, window, client).items():
            if bad:
                invalid.add(key)
            for value, quote in entries:
                entry = {"value": value, "evidence": _evidence(window, start, quote)}
                if not any(c["value"] == value for c in collected[key]):
                    if len(collected[key]) < (TEXT_LIST_MAX_ITEMS if next(f for f in fields if f["key"] == key).get("field_type") == "text_list" else 20):
                        collected[key].append(entry)
                    else:
                        invalid.add(key)
    observations = {}
    digest = hashlib.sha256(text.encode()).hexdigest()
    for field in fields:
        key = field["key"]
        candidates = collected[key]
        if field.get("field_type") == "text_list" and candidates:
            # The items found are one value; each keeps the quote that backs it.
            candidates = [{"value": [c["value"] for c in candidates], "evidence": candidates[0]["evidence"], "item_evidence": [c["evidence"] for c in candidates]}]
        status = "suggested" if candidates else "not_found"
        if len(candidates) > 1:
            status = "conflict"
        elif key in invalid and not (candidates and field.get("field_type") == "text_list"):
            status = "invalid_evidence"
        elif truncated:
            status = "partial"
        elif key in invalid:
            status = "suggested"
        elif len(candidates) == 1 and _auto_acceptable(field, candidates[0]):
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
    if not document.is_live:
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
        if not document.is_live:
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
        if not document.is_live:
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
