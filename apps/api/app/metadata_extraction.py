"""Evidence-backed custom metadata. Candidates never masquerade as manual facts."""
import hashlib
import json
import re
from datetime import date

from .document_templates import TEXT_LIST_MAX_ITEMS, metadata_search_text, validate_metadata_values
from .models import Document, JobType, ProcessingJob
from .openrouter import OpenRouterClient

EXTRACTOR_VERSION = "2"
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


# Observation status -> (extraction publishes its single candidate, trust of a published value). A value with no
# observation was typed or imported by a person. "not_found" only carries a value kept from an earlier source version.
PUBLICATION = {
    "human_verified": (True, "verified"),
    "legacy_manual": (True, "verified"),
    "auto_accepted": (True, "auto_verified"),
    "suggested": (True, "unverified"),
    "partial": (True, "unverified"),
    "conflict": (False, "blocked"),
    "invalid_evidence": (False, "blocked"),
    "not_found": (False, "unverified"),
    "not_found_confirmed": (False, None),
}
# Field review state -> what it asks of the document: nothing, a check when someone has time, or a person now.
FIELD_STATES = {"verified": "complete", "auto_verified": "complete", "not_found": "complete",
                "unverified": "unverified", "unchecked": "unverified", "blocked": "needs_attention", "missing": "needs_attention"}
REVIEW_STATUSES = ("complete", "unverified", "needs_attention")


def trust_of(observation):
    return PUBLICATION.get((observation or {}).get("status", "legacy_manual"), (False, "unverified"))[1]


def usable_values(document):
    """Values that filters, search and answers may use. A blocked value (kept from an earlier source version while the
    new extraction conflicts) stays in document_metadata so a person sees it, but nothing else relies on it."""
    observations = document.metadata_observations or {}
    return {k: v for k, v in (document.document_metadata or {}).items() if trust_of(observations.get(k)) != "blocked"}


def published_candidate(observation):
    """The candidate an extraction result puts into use, or None. Every candidate's quote was found in the source."""
    candidates = observation.get("candidates") or []
    return candidates[0] if PUBLICATION.get(observation.get("status"), (False,))[0] and len(candidates) == 1 else None


def field_state(field, values, observation):
    if trust_of(observation) == "blocked":
        return "blocked"
    if field["key"] in values:
        return trust_of(observation)
    status = observation.get("status")
    # An optional field the whole document was read for, without finding it, has nothing for a person to check.
    if status == "not_found_confirmed" or (status == "not_found" and not field.get("required") and observation.get("coverage") == "full"):
        return "not_found"
    return "missing" if field.get("required") else "unchecked"


def field_review_states(fields, values, observations):
    """Review state of every AI-extracted field, keyed by field key. The web shows these; it does not derive its own."""
    return {f["key"]: field_state(f, values or {}, (observations or {}).get(f["key"], {})) for f in fields or [] if f.get("fill_mode") == "extract"}


def update_status(document):
    needs = {FIELD_STATES[state] for state in field_review_states(document.metadata_template_fields, document.document_metadata, document.metadata_observations).values()}
    document.metadata_status = next((status for status in reversed(REVIEW_STATUSES) if status in needs), "complete")


def with_review_outcome(observation, value):
    """Record how a person's review answered the AI's top candidate. value is None when the field is not in the document.
    Confirming any other candidate is a correction of the top one."""
    if "candidates" not in observation:
        return observation
    ai_value = (observation["candidates"] or [{}])[0].get("value")
    outcome = "confirmed" if value == ai_value else "rejected" if value is None else "corrected"
    return {**observation, "review_outcome": outcome, "ai_value": ai_value}


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


_THAI_MONTHS = {name: number for number, name in enumerate(
    ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
     "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"], start=1)}
_THAI_DATE = re.compile(r"(?<!\d)(\d{1,2})\s*(" + "|".join(_THAI_MONTHS) + r")\s*(พ\s*\.?\s*ศ\s*\.?)?\s*(\d{4})(?!\d)")
_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
# A date field that names one particular date in a legal text: its quote must carry the phrase that introduces that date,
# otherwise another date in the same sentence (the gazette date, say) could stand in for it.
DATE_ANCHORS = {"signed_date": "ให้ไว้", "issued_date": "ราชกิจจานุเบกษา", "effective_date": "ใช้บังคับ"}
# The anchor introduces the date it ends at most this many (normalized) characters before, as in "ให้ไว้ ณ วันที่ ๕ มกราคม".
ANCHOR_REACH = 40


def _normalize_thai(text):
    # PDF text often spells sara am as nikhahit + sara aa.
    return text.replace("\u0e4d\u0e32", "\u0e33").translate(_THAI_DIGITS)


def _thai_date_matches(text):
    """(ISO date, start, end) for each date written as "<day> <Thai month> [พ.ศ.] <year>" in already normalized text.
    A year above 2400 is Buddhist Era."""
    matches = []
    for match in _THAI_DATE.finditer(text):
        day, month, era, year = match.groups()
        year = int(year)
        if year > 2400:
            year -= 543
        elif era:
            continue
        try:
            matches.append((date(year, _THAI_MONTHS[month], int(day)).isoformat(), match.start(), match.end()))
        except ValueError:
            continue
    return matches


def thai_dates_in(quote):
    """ISO (Gregorian) dates the quote writes."""
    return {iso for iso, _, _ in _thai_date_matches(_normalize_thai(quote))}


def _anchored_date_in(value, quote, anchor):
    """Whether the quote writes this date right after the field's anchor phrase, with no other date in between."""
    text = _normalize_thai(quote)
    matches = _thai_date_matches(text)
    for iso, start, _ in matches:
        if iso != value:
            continue
        found = text.rfind(anchor, max(0, start - ANCHOR_REACH - len(anchor)), start)
        if found != -1 and not any(found + len(anchor) <= other < start for _, other, _ in matches):
            return True
    return False


def _auto_acceptable(field, candidate):
    """Only values that are literally their own quote pass without a person. A date passes when the quote writes exactly
    that date (and its anchor phrase); a computed date never does. Numbers, booleans and graph facts need review."""
    if field.get("review_policy", "evidence") != "evidence" or field.get("graph_relationship"):
        return False
    kind, value = field.get("field_type", "text"), candidate["value"]
    if kind in {"text", "textarea", "select"}:
        return isinstance(value, str) and value in candidate["evidence"]["quote"]
    if kind == "text_list":
        return all(item in evidence["quote"] for item, evidence in zip(value, candidate["item_evidence"]))
    if kind == "date":
        quote = candidate["evidence"]["quote"]
        anchor = DATE_ANCHORS.get(field["key"])
        return _anchored_date_in(value, quote, anchor) if anchor else value in thai_dates_in(quote)
    return False


def _accepted_candidate(field, candidates):
    """The candidate that may be published without a person, or None. Among conflicting dates of a field with an
    anchor, one that alone passes the anchored check wins: the others are dates the document mentions for something
    else, or computed ones. Without an anchor nothing tells those apart, so the conflict stays."""
    passing = [c for c in candidates if _auto_acceptable(field, c)]
    if len(passing) == 1 and (len(candidates) == 1 or (field.get("field_type") == "date" and field["key"] in DATE_ANCHORS)):
        return passing[0]
    return None


def extract_candidates(fields, text, client):
    """Bound provider work and verify source quotes against the exact input version."""
    collected = {f["key"]: [] for f in fields}
    invalid = set()
    truncated = len(text) > WINDOW * MAX_WINDOWS + WINDOW_OVERLAP  # the last window reads the overlap past the cap
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
        accepted = _accepted_candidate(field, candidates) if not truncated and key not in invalid else None
        status = "suggested" if candidates else "not_found"
        if len(candidates) > 1 and not accepted:
            status = "conflict"
        elif key in invalid and not (candidates and field.get("field_type") == "text_list"):
            status = "invalid_evidence"
        elif truncated:
            status = "partial"
        elif key in invalid:
            status = "suggested"
        elif accepted:
            status, candidates = "auto_accepted", [accepted]
        observations[key] = {"status": status, "origin": "document_extraction", "candidates": candidates,
                             "content_version": digest, "extractor_version": EXTRACTOR_VERSION,
                             "coverage": "partial" if truncated else "full", "locked": False}
    return observations


def reevaluate_observations(db, document):
    """Apply the current date rules and the publication table to stored candidates without asking the provider again,
    and refresh the review status. The caller holds the KB graph lock and the document row lock. Returns the number of
    fields whose published value or status changed."""
    from .services import sync_document_metadata_graph, sync_document_metadata_values

    digest = hashlib.sha256((document.extracted_text or "").encode()).hexdigest()
    values = dict(document.document_metadata or {})
    observations = dict(document.metadata_observations or {})
    changed = 0
    for field in document.metadata_template_fields or []:
        observation = observations.get(field["key"], {})
        if (field.get("fill_mode") != "extract" or observation.get("locked") or observation.get("origin") != "document_extraction"
                or observation.get("content_version") != digest
                or observation.get("template_version", document.metadata_template_version) != document.metadata_template_version):
            continue
        promoted = False
        if field.get("field_type") == "date" and observation.get("status") in {"suggested", "conflict"} and observation.get("coverage") == "full":
            if accepted := _accepted_candidate(field, observation.get("candidates") or []):
                observation = observations[field["key"]] = {**observation, "status": "auto_accepted", "candidates": [accepted]}
                promoted = True
        candidate = published_candidate(observation)
        republish = bool(candidate) and values.get(field["key"]) != candidate["value"]
        if republish:
            values[field["key"]] = candidate["value"]
        changed += promoted or republish
    if changed:
        document.document_metadata = values
        document.metadata_observations = observations
        document.metadata_revision += 1
        document.metadata_search_text = metadata_search_text(document.metadata_template_fields, usable_values(document))
        sync_document_metadata_values(db, document)
        sync_document_metadata_graph(db, document)
    # A queued, running or failed extraction owns the status until it finishes. needs_review predates the review states.
    if changed or document.metadata_status in {"needs_review", *REVIEW_STATUSES}:
        update_status(document)
    return changed


def process_metadata_job(db, job):
    """Independent retry and compare-and-set publication after slow provider work.

    The job was claimed by job_queue.claim_next_job (running, with a claim token).
    """
    from .job_queue import ClaimLost, lock_knowledge_base_graph
    from .retry_policy import PROCESSING_RETRY_POLICIES, schedule_retry
    from .services import sync_document_metadata_graph, sync_document_metadata_values

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
        # Lock order Knowledge Base -> document (same as the graph-writing jobs):
        # the KB graph lock is taken before this transaction locks the document row.
        lock_knowledge_base_graph(db, job.knowledge_base_id or document.knowledge_base_id)
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
            if candidate := published_candidate(observation):
                values[key] = candidate["value"]
        document.document_metadata = values
        document.metadata_observations = {**(document.metadata_observations or {}), **observations}
        document.metadata_revision += 1
        document.metadata_search_text = metadata_search_text(document.metadata_template_fields, usable_values(document))
        update_status(document)
        sync_document_metadata_values(db, document)
        sync_document_metadata_graph(db, document)
        job.status, job.current_stage, job.progress_percent = "completed", "completed", 100
        job.error_code, job.error_message = None, None
        db.commit()
    except ClaimLost:
        raise
    except Exception as exc:
        db.rollback()
        document = db.query(Document).filter_by(id=job.document_id).populate_existing().with_for_update().one()
        if not document.is_live:
            job.status = "cancelled"
        else:
            # Provider outages and rate limits get the long, non-blocking provider schedule.
            retry = schedule_retry(job, PROCESSING_RETRY_POLICIES.get(str(exc))) if str(exc) in {"OPENROUTER_UNAVAILABLE", "OPENROUTER_CREDITS_EXHAUSTED"} else False
            job.status = "queued" if retry else "failed"
            job.current_stage = "retry_wait" if retry else "failed"
            job.error_code = str(exc) if str(exc) in {"OPENROUTER_UNAVAILABLE", "OPENROUTER_AUTH_FAILED", "OPENROUTER_CREDITS_EXHAUSTED", "OPENROUTER_REQUEST_REJECTED", "OPENROUTER_API_KEY_NOT_CONFIGURED", "METADATA_TEXT_NOT_READY", "METADATA_EXTRACTION_INVALID_RESPONSE"} else "METADATA_EXTRACTION_FAILED"
            job.error_message = "Metadata extraction did not complete; document search remains available."
            if document.metadata_revision != revision:
                update_status(document)
            else:
                document.metadata_status = "queued" if retry else "failed"
        db.commit()
