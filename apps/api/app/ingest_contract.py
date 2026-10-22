"""What an Ingest API client can read about the platform's limits, error codes and queue, so it can check before
sending, pace itself, and tell a document that is waiting from one that needs a person."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from .config import get_settings
from .document_templates import METADATA_MAX_CHARS
from .ingest_text import MAX_TEXT_BYTES
from .models import Document, ProcessingJob, TokenKey
from .provider_holds import HOLD_CODES, held_job_types, hold_view

INGEST_MAX_BATCH_FILES = 20
EXTERNAL_ID_MAX_CHARS = 200
LOOKUP_MAX_ITEMS = 100
LIST_MAX_LIMIT = 100
TERMINAL_DOCUMENT_STATUSES = ("completed", "failed", "ocr_required")


@dataclass(frozen=True)
class ErrorCode:
    # "request": the HTTP answer to a call. "processing": a document's or job's error_code after a 202.
    stage: str
    http_status: int | None
    # The same request, or the same document, can succeed later without any change on the client's side.
    retryable: bool
    # resend_later | skip | fix_request | fix_file | wait | contact_admin
    client_action: str
    description: str


def _request(status: int, action: str, description: str, retryable: bool = False) -> ErrorCode:
    return ErrorCode("request", status, retryable, action, description)


def _processing(action: str, description: str, retryable: bool = False) -> ErrorCode:
    return ErrorCode("processing", None, retryable, action, description)


ERROR_CODES: dict[str, ErrorCode] = {
    "AUTH_TOKEN_MISSING": _request(401, "fix_request", "No Authorization: Bearer header was sent."),
    "AUTH_TOKEN_INVALID": _request(401, "contact_admin", "The token is wrong or disabled."),
    "AUTH_TOKEN_REVOKED": _request(401, "contact_admin", "The token was revoked."),
    "AUTH_TOKEN_EXPIRED": _request(401, "contact_admin", "The token has expired."),
    "AUTH_SCOPE_NOT_ALLOWED": _request(403, "contact_admin", "The token has no documents:write scope."),
    "AUTH_KNOWLEDGE_BASE_NOT_ALLOWED": _request(403, "contact_admin", "The token has no Knowledge Base to ingest into."),
    "KNOWLEDGE_BASE_NOT_FOUND": _request(404, "fix_request", "No such Knowledge Base, or it is outside this token's scope."),
    "KNOWLEDGE_BASE_DISABLED": _request(409, "contact_admin", "The Knowledge Base is not active."),
    "DOCUMENT_NOT_FOUND": _request(404, "fix_request", "No such document, or it is outside this token's scope."),
    "FILE_DUPLICATE": _request(409, "skip", "The same content is already in this Knowledge Base; error.document_id names it."),
    "EXTERNAL_ID_EXISTS": _request(409, "skip", "A document with this external_id is already in this Knowledge Base; error.document_id names it."),
    "EXTERNAL_ID_INVALID": _request(400, "fix_request", f"external_id must be 1-{EXTERNAL_ID_MAX_CHARS} characters."),
    "FILE_TYPE_NOT_SUPPORTED": _request(400, "fix_file", "The file extension is not accepted; see GET /ingest/limits."),
    "FILE_MIME_TYPE_NOT_SUPPORTED": _request(400, "fix_request", "The MIME type does not match the extension."),
    "FILE_TOO_LARGE": _request(413, "fix_file", "The file or text is larger than the limit in GET /ingest/limits."),
    "TEXT_EMPTY": _request(400, "fix_request", "The text is empty."),
    "DOCUMENT_TYPE_INVALID": _request(400, "fix_request", "document_type is not one of the known profiles."),
    "DOCUMENT_TYPE_ID_CONFLICT": _request(400, "fix_request", "document_type_id and template_id name different types."),
    "DOCUMENT_TEMPLATE_NOT_FOUND": _request(400, "fix_request", "The document type id is not in this Knowledge Base."),
    "DOCUMENT_METADATA_INVALID": _request(400, "fix_request", "metadata_json is not a JSON object, or a value has the wrong type."),
    "DOCUMENT_METADATA_FIELD_UNKNOWN": _request(400, "fix_request", "metadata_json has a key the document type does not declare."),
    "DOCUMENT_METADATA_REQUIRED": _request(400, "fix_request", "A field the document type requires is missing."),
    "DOCUMENT_METADATA_TOO_LARGE": _request(400, "fix_request", "metadata_json is larger than the limit in GET /ingest/limits."),
    "REFERENCE_FILE_CONFLICT": _request(400, "fix_request", "Send reference_file or reference_pdf, not both."),
    "REFERENCE_FILE_REQUIRES_MARKDOWN": _request(400, "fix_request", "A reference file can only be attached to a .md document."),
    "REFERENCE_PDF_REQUIRES_MARKDOWN": _request(400, "fix_request", "A reference PDF can only be attached to a .md document."),
    "REFERENCE_FILE_TYPE_NOT_SUPPORTED": _request(400, "fix_file", "The reference file's extension is not accepted."),
    "REFERENCE_FILE_INVALID": _request(400, "fix_file", "The reference file is empty or its content does not match its extension."),
    "REFERENCE_PDF_INVALID": _request(400, "fix_file", "The reference PDF is not a PDF."),
    "REFERENCE_FILE_ALREADY_EXISTS": _request(409, "skip", "The document already has a reference file."),
    "REFERENCE_PDF_ALREADY_EXISTS": _request(409, "skip", "The document already has a reference file."),
    "BATCH_FILES_REQUIRED": _request(400, "fix_request", "The batch has no files."),
    "BATCH_TOO_MANY_FILES": _request(400, "fix_request", f"A batch holds at most {INGEST_MAX_BATCH_FILES} files."),
    "DOCUMENT_PAGE_INVALID": _request(400, "fix_request", f"limit must be 1-{LIST_MAX_LIMIT} and offset non-negative."),
    "LOOKUP_INVALID": _request(400, "fix_request", f"Send 1-{LOOKUP_MAX_ITEMS} values; a checksum is 64 hex characters."),
    "UPLOAD_FAILED": ErrorCode("request", None, True, "resend_later", "One file in a batch's results could not be queued; send it again."),
    "MCP_RATE_LIMITED": _request(429, "resend_later", "Over the token's requests per minute; wait Retry-After seconds.", retryable=True),
    "MCP_CONCURRENCY_LIMITED": _request(429, "resend_later", "Over the token's concurrent requests; wait Retry-After seconds.", retryable=True),
    "MCP_LIMIT_STORE_UNAVAILABLE": _request(503, "resend_later", "The rate-limit counter is briefly unavailable.", retryable=True),

    "RETRIEVAL_ENGINE_BUSY": _processing("wait", "The search index is deleting other documents; retried for up to about 38 minutes.", True),
    "RETRIEVAL_ENGINE_UNAVAILABLE": _processing("wait", "The search index could not be reached; retried for up to about 27 minutes.", True),
    "RETRIEVAL_ENGINE_TIMEOUT": _processing("wait", "The search index stopped making progress on the document; retried.", True),
    "RETRIEVAL_ENGINE_REJECTED": _processing("wait", "The search index refused the document; retried, then failed.", True),
    "RETRIEVAL_ENGINE_DUPLICATE": _processing("skip", "The same text is already indexed in this Knowledge Base."),
    "LLM_UNAVAILABLE": _processing("wait", "The AI provider did not answer; retried for about half an hour.", True),
    "EMBEDDING_UNAVAILABLE": _processing("wait", "The embedding server did not answer; retried for about half an hour.", True),
    "EXTERNAL_OCR_UNAVAILABLE": _processing("wait", "The OCR service could not be reached; retried.", True),
    "EXTERNAL_OCR_TIMEOUT": _processing("wait", "The OCR service took too long; retried.", True),
    "OCR_CHAIN_FAILED": _processing("wait", "Every OCR engine failed on the file; retried, then failed.", True),
    "OCR_REQUIRED": _processing("contact_admin", "The file is a scan and OCR is switched off."),
    "TEXT_EXTRACTION_FAILED": _processing("fix_file", "No text could be read from the file."),
    "TEXT_EXTRACTION_EMPTY": _processing("fix_file", "The file holds no text."),
    "ANYDOC_UNAVAILABLE": _processing("contact_admin", "The document converter is not available."),
    "EMBEDDING_NOT_CONFIGURED": _processing("contact_admin", "Embeddings are not configured on the platform."),
    "EMBEDDING_DIMENSION_MISMATCH": _processing("contact_admin", "The embedding model does not match the index."),
    "EMBEDDING_INVALID_RESPONSE": _processing("contact_admin", "The embedding server answered with something unusable."),
    "LLM_API_KEY_NOT_CONFIGURED": _processing("contact_admin", "The AI provider key is not configured."),
    "LLM_INVALID_RESPONSE": _processing("contact_admin", "The AI provider answered with something unusable."),
    "METADATA_EXTRACTION_INVALID_RESPONSE": _processing("contact_admin", "Metadata extraction failed; the document itself is searchable."),
    "METADATA_TEXT_NOT_READY": _processing("wait", "Metadata extraction waits for the document's text.", True),
    "DOCUMENT_DELETED": _processing("skip", "The document was deleted before its job ran."),
    "REMOTE_INDEX_PURGE_FAILED": _processing("contact_admin", "A deleted document could not be removed from the search index."),
    **{code: _processing("wait", "The AI provider is out of credits or its key failed; jobs pause and resume on their own.", True)
       for code in HOLD_CODES},
}
_UNKNOWN = _processing("contact_admin", "Not in this catalogue.")


def error_catalog() -> list[dict]:
    return [{"code": code, **asdict(entry)} for code, entry in ERROR_CODES.items()]


def error_code_info(code: str | None) -> ErrorCode | None:
    return ERROR_CODES.get(code, _UNKNOWN) if code else None


def limits_view(token: TokenKey) -> dict:
    from .services import ALLOWED_MIME_TYPES, REFERENCE_FILE_EXTENSIONS, SUPPORTED_EXTENSIONS
    max_bytes = get_settings().max_file_size_mb * 1024 * 1024
    return {
        "file": {"max_bytes": max_bytes, "extensions": sorted(SUPPORTED_EXTENSIONS),
                 "mime_types": {ext: sorted(ALLOWED_MIME_TYPES[ext]) for ext in sorted(SUPPORTED_EXTENSIONS)},
                 "octet_stream_accepted": True},
        "reference_file": {"max_bytes": max_bytes, "extensions": sorted(REFERENCE_FILE_EXTENSIONS), "document_extension": ".md"},
        "text": {"max_bytes": MAX_TEXT_BYTES, "title_max_chars": 255},
        "batch": {"max_files": INGEST_MAX_BATCH_FILES},
        "metadata_json": {"max_chars": METADATA_MAX_CHARS},
        "external_id": {"max_chars": EXTERNAL_ID_MAX_CHARS},
        "lookup": {"max_items": LOOKUP_MAX_ITEMS},
        "document_list": {"max_limit": LIST_MAX_LIMIT},
        "token": {"knowledge_base_id": token.allowed_ingest_knowledge_base_id, "requests_per_minute": token.requests_per_minute,
                  "max_concurrent_requests": token.max_concurrent_requests,
                  "expires_at": token.expires_at.isoformat() + "Z" if token.expires_at else None},
        "polling": {"initial_seconds": 5, "max_seconds": 60},
    }


def _utc(moment: datetime | None) -> str | None:
    return moment.isoformat() + "Z" if moment else None


def _job_counts(query, now: datetime) -> dict:
    counts = dict(query.with_entities(ProcessingJob.status, func.count()).group_by(ProcessingJob.status).all())
    waiting = query.filter(ProcessingJob.status == "queued", ProcessingJob.next_attempt_at > now).count()
    return {"queued": counts.get("queued", 0), "running": counts.get("running", 0), "waiting_to_retry": waiting}


def queue_view(db: Session, knowledge_base_id: str) -> dict:
    """The Active Queue as counts: every Knowledge Base's pending work (no names or ids) and this one's in detail."""
    now = datetime.utcnow()
    active = db.query(ProcessingJob).filter(ProcessingJob.status.in_(("queued", "running")))
    own = active.filter(ProcessingJob.knowledge_base_id == knowledge_base_id)
    statuses = dict(db.query(Document.status, func.count()).filter(Document.knowledge_base_id == knowledge_base_id, Document.live())
                    .group_by(Document.status).all())
    oldest = own.with_entities(func.min(ProcessingJob.created_at)).scalar()
    held = held_job_types(db)
    return {
        "knowledge_base_id": knowledge_base_id,
        "generated_at": _utc(now),
        "processing": "paused" if held else "running",
        "holds": [{"reason_code": hold["reason_code"], "held_since": hold["held_since"], "next_check_at": hold["next_probe_at"]}
                  for hold in hold_view(db)],
        "active_queue": {
            "documents": active.with_entities(func.count(func.distinct(ProcessingJob.document_id))).scalar(),
            "jobs": _job_counts(active, now),
        },
        "knowledge_base": {
            "active_documents": own.with_entities(func.count(func.distinct(ProcessingJob.document_id))).scalar(),
            "jobs": _job_counts(own, now),
            "oldest_active_job_at": _utc(oldest),
            "documents_by_status": statuses,
            "in_flight_documents": sum(count for status, count in statuses.items() if status not in TERMINAL_DOCUMENT_STATUSES),
        },
    }


def waiting_view(job: ProcessingJob | None, now: datetime | None = None) -> dict:
    """Why a queued job has not run yet: the error it waits out and when it runs next."""
    if job is None or job.status != "queued":
        return {"waiting_for": None, "next_attempt_at": None}
    now = now or datetime.utcnow()
    due = job.next_attempt_at if job.next_attempt_at and job.next_attempt_at > now else None
    return {"waiting_for": job.error_code, "next_attempt_at": _utc(due)}
