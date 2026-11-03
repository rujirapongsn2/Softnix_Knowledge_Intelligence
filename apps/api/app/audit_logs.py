"""Scoped document audit history for the operations GUI."""
import base64
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from .models import AuditLog, Document, KnowledgeBase, ROLE_ADMIN, TokenKey, User
from .security import kb_ids_in_group_scope

DETAIL_KEYS = {
    "metadata_fields", "fields", "field_key", "action", "revision", "published_at", "document_type",
    "filename", "error_code", "count", "queued", "force", "batch", "changed_fields",
}


def invalid(message: str):
    raise HTTPException(400, {"code": "AUDIT_FILTER_INVALID", "message": message, "retryable": False})


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        invalid("Use an ISO timestamp with a timezone.")


def utc(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def decode_cursor(value: str | None):
    if not value:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
        if not isinstance(payload, list) or len(payload) != 2:
            raise ValueError()
        stamp, row_id = payload
        if not isinstance(stamp, str) or not isinstance(row_id, str) or not 1 <= len(row_id) <= 36:
            raise ValueError()
        timestamp = parse_time(stamp)
        if timestamp is None:
            raise ValueError()
        return timestamp, row_id
    except (ValueError, TypeError, UnicodeError):
        invalid("Invalid audit cursor.")


def document_audit_page(db: Session, user: User, *, knowledge_base_id=None, document_id=None,
                        action=None, actor_type=None, actor_id=None, channel=None, from_ts=None, to_ts=None,
                        search=None, cursor=None, limit=50) -> dict:
    if not 1 <= limit <= 100:
        invalid("Limit must be between 1 and 100.")
    if actor_type not in (None, "user", "token", "system") or channel not in (None, "gui", "ingest_api", "system"):
        invalid("Invalid actor type or channel.")
    for value in (knowledge_base_id, document_id, actor_id):
        if value and len(value) > 100:
            invalid("Identifier is too long.")
    if (action and len(action) > 100 or search and len(search) > 200 or cursor and len(cursor) > 500
            or from_ts and len(from_ts) > 50 or to_ts and len(to_ts) > 50):
        invalid("Filter is too long.")
    start, end = parse_time(from_ts), parse_time(to_ts)
    if start and end and start > end:
        invalid("Start time must be before end time.")
    after = decode_cursor(cursor)
    meta = AuditLog.metadata_json
    doc_id = case((AuditLog.target_type == "document", AuditLog.target_id), else_=meta["document_id"].as_string())
    kb_id = func.coalesce(Document.knowledge_base_id, meta["knowledge_base_id"].as_string(),
                          case((AuditLog.target_type == "knowledge_base", AuditLog.target_id)))
    token_id = func.coalesce(meta["token_id"].as_string(), case((AuditLog.target_type == "token", AuditLog.target_id)))
    token_event = or_(token_id.is_not(None), meta["token_name"].as_string().is_not(None),
                      meta["transport"].as_string() == "ingest_api", AuditLog.action == "document.ingest.rejected")
    actor_kind = case((AuditLog.actor_user_id.is_not(None), "user"), (token_event, "token"), else_="system")
    actor_key = case((AuditLog.actor_user_id.is_not(None), AuditLog.actor_user_id),
                     (token_event, func.coalesce(token_id, "unknown-token")), else_="system")
    actor_name = case((AuditLog.actor_user_id.is_not(None), func.coalesce(User.username, AuditLog.actor_user_id)),
                      (token_event, func.coalesce(TokenKey.name, meta["token_name"].as_string(), "Ingest API")), else_="System")
    event_channel = case((token_event, "ingest_api"), (AuditLog.actor_user_id.is_not(None), "gui"), else_="system")
    kb_name = func.coalesce(KnowledgeBase.name, meta["knowledge_base_name"].as_string(), kb_id)
    filename = func.coalesce(meta["filename"].as_string(), Document.original_filename)
    title = func.coalesce(meta["document_title"].as_string(), Document.title, filename, doc_id)
    base = (db.query(AuditLog, Document, KnowledgeBase, kb_id.label("kb_id"), kb_name.label("kb_name"),
                     doc_id.label("document_id"), filename.label("filename"), title.label("title"),
                     actor_kind.label("actor_type"), actor_key.label("actor_id"), actor_name.label("actor_name"),
                     event_channel.label("channel"))
            .outerjoin(Document, Document.id == doc_id).outerjoin(KnowledgeBase, KnowledgeBase.id == kb_id)
            .outerjoin(User, User.id == AuditLog.actor_user_id).outerjoin(TokenKey, TokenKey.id == token_id)
            .filter(AuditLog.action.startswith("document.")))
    if user.role != ROLE_ADMIN:
        base = base.filter(kb_id.in_(kb_ids_in_group_scope(db, user)))

    options = {
        "knowledge_bases": [{"id": row[0], "name": row[1]} for row in base.with_entities(kb_id, kb_name).filter(kb_id.is_not(None)).distinct().order_by(kb_name).all()],
        "actors": [{"type": row[0], "id": row[1], "name": row[2]} for row in base.with_entities(actor_kind, actor_key, func.max(actor_name)).group_by(actor_kind, actor_key).order_by(func.max(actor_name)).all()],
        "actions": [row[0] for row in base.with_entities(AuditLog.action).distinct().order_by(AuditLog.action).all()],
    }
    query = base
    for expression, value in ((kb_id, knowledge_base_id), (doc_id, document_id), (AuditLog.action, action),
                              (actor_kind, actor_type), (actor_key, actor_id), (event_channel, channel)):
        if value:
            query = query.filter(expression == value)
    if start:
        query = query.filter(AuditLog.created_at >= start)
    if end:
        query = query.filter(AuditLog.created_at <= end)
    if search and search.strip():
        needle = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.filter(or_(*(field.ilike(f"%{needle}%", escape="\\") for field in
                                  (title, filename, doc_id, kb_name, actor_name, AuditLog.action))))
    total = query.count()
    if after:
        query = query.filter(or_(AuditLog.created_at < after[0],
                                 and_(AuditLog.created_at == after[0], AuditLog.id < after[1])))
    rows = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for row in rows:
        log, doc, kb = row[:3]
        details = {key: value for key, value in (log.metadata_json or {}).items() if key in DETAIL_KEYS
                   and isinstance(value, (str, int, float, bool, list, type(None)))}
        # Lists describe field names only. Reject nested payloads rather than exposing metadata values.
        details = {key: value for key, value in details.items() if not isinstance(value, list) or all(isinstance(item, str) for item in value)}
        items.append({"id": log.id, "created_at": utc(log.created_at), "action": log.action,
                      "knowledge_base": {"id": row.kb_id, "name": row.kb_name} if row.kb_id else None,
                      "document": {"id": row.document_id, "title": row.title, "filename": row.filename,
                                   "deleted": bool(doc and not doc.is_live),
                                   "available": bool(doc and doc.is_live and kb and kb.deleted_at is None)} if row.document_id else None,
                      "actor": {"type": row.actor_type, "id": row.actor_id, "name": row.actor_name},
                      "channel": row.channel, "details": details})
    next_cursor = None
    if has_more and rows:
        last = rows[-1][0]
        next_cursor = base64.urlsafe_b64encode(json.dumps([utc(last.created_at), last.id]).encode()).decode()
    return {"items": items, "total": total, "next_cursor": next_cursor, "has_more": has_more,
            "limit": limit, "filter_options": options}
