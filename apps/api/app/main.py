from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
import hashlib
import json
import re
import time
import unicodedata

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
import httpx
import redis
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .config import get_settings
from .audit import record_audit, record_mcp_error_trace, record_retrieval_execution
from .db import Base, engine, get_db, SessionLocal
from .graph_store import Neo4jGraphStore
from .legal_registry import provision_number_matches, resolve_instrument_statuses
from .models import AuditLog, Document, DocumentMetadataTemplate, Entity, EntitySource, GraphNodeLayout, GraphProjectionEvent, Group, KbOwner, KnowledgeBase, LegalFamily, LegalInstrument, LegalInstrumentRelation, ProcessingJob, QueryFeedback, QueryResult, Relationship, RelationshipSource, ROLE_ADMIN, ROLES, TokenKey, TraceRun, TraceSpan, User
from .document_templates import SYSTEM_TEMPLATE_CODES, SYSTEM_TEMPLATE_NAMES, custom_template_fields, list_templates, merge_profile_fields, metadata_search_text, resolve_template, template_code, validate_metadata_values
from .observability import metrics, now
from .openrouter import OpenRouterClient
from .mcp_limits import McpLimitExceeded, mcp_limiter
from .request_budget import reset_deadline, set_deadline
from .retention import prune_observability
from .schemas import DocumentInventoryRequest, DocumentMetadataTemplateCreate, DocumentMetadataTemplateOut, DocumentMetadataTemplateUpdate, DocumentMetadataUpdate, DocumentOut, DocumentPageOut, EntityCreate, EntityOut, EntityUpdate, GraphLayoutUpdate, GroupCreate, GroupOut, GroupUpdate, ImpactRequest, KnowledgeBaseCreate, KnowledgeBaseIconUpdate, KnowledgeBaseOut, LegalInstrumentOut, LegalInstrumentUpdate, LegalMetadataUpdate, LegalRelationshipReview, LoginRequest, PasswordChange, PasswordReset, QueryFeedbackCreate, QueryRequest, RelationshipCreate, RelationshipOut, RelationshipUpdate, RetrievalConfigUpdate, TokenCreate, TokenCreated, TokenOut, UserCreate, UserOut, UserUpdate
from pydantic import BaseModel, Field
from .security import INGEST_SCOPE, assert_kb_access, authorize, bearer_token, create_session_token, create_token_secret, current_admin, ingest_token, kb_ids_visible_to, kb_ids_visible_to_group, password_hash, refresh_admin, require_admin, require_manager, token_digest, token_visible_to, verify_password
from .services import DEFAULT_RETRIEVAL_CONFIG, analyze_impact, build_document_inventory_result, build_query_result, build_retrieval_plan, create_document_job, create_entity, create_relationship, entity_graph, process_next_job, queue_embedding_reindex, resolve_entity, sync_document_metadata_graph, sync_document_metadata_values, sync_legal_document_graph, sync_legal_instrument_relation_review, sync_lightrag_document_graph

app = FastAPI(title="Softnix Knowledge Intelligence Platform", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:8080", "http://localhost:8081"], allow_credentials=True,
                   allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type"])


REQUEST_TRANSACTION_ACTION = "request.transaction"
# Health probes and reads of the logging APIs intentionally do not create a new
# transaction record. This keeps the operator view useful rather than filling it
# with its own polling traffic.
REQUEST_LOG_EXCLUDED_PATHS = {"/health", "/ready", "/metrics", "/api/v1/audit-logs", "/api/v1/logs/transactions", "/api/v1/traces"}


def record_request_transaction(request: Request, request_id: str, status_code: int, duration_seconds: float) -> None:
    """Persist operator-safe request metadata without ever retaining request secrets or bodies."""
    path = request.url.path
    if path in REQUEST_LOG_EXCLUDED_PATHS or (not path.startswith("/api/") and path != "/mcp"):
        return
    bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
    auth_type = ("ingest_token" if path.startswith("/api/v1/ingest") else "mcp_token") if bearer else (
        "admin_session" if "skip_access" in request.cookies else "anonymous"
    )
    db = SessionLocal()
    try:
        record_audit(db, REQUEST_TRANSACTION_ACTION, target_type="http_request", target_id=request_id, metadata={
            "request_id": request_id,
            "method": request.method,
            "path": path,
            "status_code": status_code,
            "duration_ms": round(duration_seconds * 1000, 1),
            "authentication": auth_type,
        })
        db.commit()
    except Exception:
        # Observability must never change a successful request into an error.
        db.rollback()
    finally:
        db.close()


@app.middleware("http")
async def request_observability(request: Request, call_next):
    started = now()
    supplied_request_id = str(request.headers.get("X-Request-ID") or "").strip()
    request_id = supplied_request_id[:36] or __import__("uuid").uuid4().hex
    request.state.request_id = request_id
    try:
        response = await call_next(request)
    except Exception:
        duration = now() - started
        metrics.observe(request.method, request.url.path, 500, duration)
        record_request_transaction(request, request_id, 500, duration)
        raise
    duration = now() - started
    metrics.observe(request.method, request.url.path, response.status_code, duration)
    record_request_transaction(request, request_id, response.status_code, duration)
    response.headers["X-Request-ID"] = request_id
    return response


@app.on_event("startup")
def bootstrap() -> None:
    settings = get_settings()
    Path(settings.file_storage_path).mkdir(parents=True, exist_ok=True)
    if settings.app_env in {"development", "test"}:
        Base.metadata.create_all(engine)
    from .db import SessionLocal
    with SessionLocal() as db:
        if not db.query(User).filter_by(username=settings.initial_admin_username).first():
            db.add(User(username=settings.initial_admin_username,
                        password_hash=password_hash(settings.initial_admin_password),
                        role=ROLE_ADMIN))
            db.commit()


@app.exception_handler(HTTPException)
async def structured_http_error(_: Request, exc: HTTPException):
    from fastapi.responses import JSONResponse
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "VALIDATION_ERROR", "message": str(exc.detail), "retryable": False}
    return JSONResponse(status_code=exc.status_code, content={"status": "error", "error": detail})


@app.get("/health")
def health(): return {"status": "healthy"}


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics():
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/ready")
def ready(db: Session = Depends(get_db)):
    settings = get_settings()
    dependencies, failures = {}, {}
    try:
        db.execute(__import__("sqlalchemy").text("SELECT 1")); dependencies["database"] = "ready"
    except Exception: failures["database"] = "unavailable"
    if settings.redis_url:
        try:
            redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2).ping(); dependencies["redis"] = "ready"
        except redis.RedisError: failures["redis"] = "unavailable"
    if settings.neo4j_http_url:
        try:
            if Neo4jGraphStore().check(): dependencies["neo4j"] = "ready"
        except (httpx.HTTPError, RuntimeError): failures["neo4j"] = "unavailable"
    if settings.lightrag_base_url:
        try:
            headers = {"X-API-Key": settings.lightrag_api_key} if settings.lightrag_api_key else {}
            httpx.get(f"{settings.lightrag_base_url.rstrip('/')}/health", headers=headers, timeout=3).raise_for_status()
            dependencies["lightrag"] = "ready"
        except httpx.HTTPError: failures["lightrag"] = "unavailable"
    if settings.softnix_ocr_base_url and settings.softnix_ocr_token:
        try:
            probe = httpx.get(f"{settings.softnix_ocr_base_url.rstrip('/')}/v3/queue-info",
                              headers={"Authorization": f"Bearer {settings.softnix_ocr_token}"},
                              verify=not settings.softnix_ocr_insecure_tls, timeout=5)
            probe.raise_for_status()
            dependencies["ocr_chain"] = "ready"
        except (httpx.HTTPError, OSError):
            failures["ocr_chain"] = "unavailable"
    if failures:
        raise HTTPException(503, {"code": "DEPENDENCY_UNAVAILABLE", "message": "One or more dependencies are unavailable", "retryable": True, "details": failures})
    return {"status": "ready", "dependencies": dependencies}


@app.get("/api/v1/system/status")
def system_status(_: User = Depends(current_admin), db: Session = Depends(get_db)):
    """Authenticated dependency status for the administrator operations view."""
    return ready(db)


@app.post("/api/v1/auth/login")
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(username=payload.username).first()
    if not user or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, {"code": "AUTH_TOKEN_INVALID", "message": "Invalid username or password", "retryable": False})
    s = get_settings()
    response.set_cookie("skip_access", create_session_token(user, "access", timedelta(minutes=s.access_token_minutes)), httponly=True, secure=s.cookie_secure, samesite="lax", max_age=s.access_token_minutes * 60)
    response.set_cookie("skip_refresh", create_session_token(user, "refresh", timedelta(days=s.refresh_token_days)), httponly=True, secure=s.cookie_secure, samesite="lax", max_age=s.refresh_token_days * 86400)
    record_audit(db, "auth.login", user.id, "user", user.id); db.commit()
    return {"status": "success", "user": {"id": user.id, "username": user.username}}


@app.post("/api/v1/auth/logout")
def logout(response: Response):
    response.delete_cookie("skip_access"); response.delete_cookie("skip_refresh"); return {"status": "success"}


@app.post("/api/v1/auth/refresh")
def refresh_session(response: Response, user: User = Depends(refresh_admin)):
    settings = get_settings()
    response.set_cookie("skip_access", create_session_token(user, "access", timedelta(minutes=settings.access_token_minutes)), httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=settings.access_token_minutes * 60)
    response.set_cookie("skip_refresh", create_session_token(user, "refresh", timedelta(days=settings.refresh_token_days)), httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=settings.refresh_token_days * 86400)
    return {"status": "success", "user": {"id": user.id, "username": user.username}}


@app.get("/api/v1/auth/me")
def me(user: User = Depends(current_admin), db: Session = Depends(get_db)):
    group = db.get(Group, user.group_id) if user.group_id else None
    return {"id": user.id, "username": user.username, "display_name": user.display_name,
            "role": user.role, "group": {"id": group.id, "name": group.name} if group else None}


@app.post("/api/v1/auth/change-password")
def change_password(payload: PasswordChange, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(401, {"code": "AUTH_PASSWORD_INVALID", "message": "Current password is incorrect.", "retryable": False})
    user.password_hash = password_hash(payload.new_password)
    user.credentials_version = (user.credentials_version or 0) + 1
    record_audit(db, "auth.password_changed", user.id, "user", user.id)
    db.commit()
    return {"status": "success"}


@app.get("/api/v1/users", response_model=list[UserOut])
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(User).order_by(User.created_at.asc()).all()


@app.post("/api/v1/users", response_model=UserOut)
def create_user(payload: UserCreate, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    if db.query(User.id).filter_by(username=payload.username).first():
        raise HTTPException(409, {"code": "USER_EXISTS", "message": "Username already exists.", "retryable": False})
    if payload.group_id and not db.get(Group, payload.group_id):
        raise HTTPException(404, {"code": "GROUP_NOT_FOUND", "message": "Group not found.", "retryable": False})
    row = User(username=payload.username, password_hash=password_hash(payload.password),
               display_name=payload.display_name, role=payload.role, group_id=payload.group_id)
    db.add(row)
    record_audit(db, "user.create", user.id, "user", row.id, {"username": row.username, "role": row.role})
    db.commit(); db.refresh(row)
    return row


@app.patch("/api/v1/users/{user_id}", response_model=UserOut)
def update_user(user_id: str, payload: UserUpdate, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    row = db.get(User, user_id)
    if not row:
        raise HTTPException(404, "User not found")
    if payload.group_id is not None and payload.group_id and not db.get(Group, payload.group_id):
        raise HTTPException(404, {"code": "GROUP_NOT_FOUND", "message": "Group not found.", "retryable": False})
    changes = payload.model_dump(exclude_unset=True)
    # Last-admin guard: an admin may not demote or deactivate themselves —
    # either would leave the system without a usable administrator.
    if row.id == user.id and (("role" in changes and changes["role"] != ROLE_ADMIN) or ("is_active" in changes and changes["is_active"] is False)):
        raise HTTPException(409, {"code": "LAST_ADMIN_GUARD", "message": "Admins cannot demote or deactivate their own account.", "retryable": False})
    if "password" in changes:
        del changes["password"]  # never via PATCH; use reset-password
    for key, value in changes.items():
        setattr(row, key, value)
    record_audit(db, "user.update", user.id, "user", row.id, {"fields": sorted(changes)})
    db.commit(); db.refresh(row)
    return row


@app.post("/api/v1/users/{user_id}/reset-password")
def reset_password(user_id: str, payload: PasswordReset, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    row = db.get(User, user_id)
    if not row:
        raise HTTPException(404, "User not found")
    row.password_hash = password_hash(payload.password)
    row.credentials_version = (row.credentials_version or 0) + 1
    record_audit(db, "user.password_reset", user.id, "user", row.id)
    db.commit()
    return {"status": "success"}


@app.get("/api/v1/groups", response_model=list[GroupOut])
def list_groups(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(Group).order_by(Group.created_at.asc()).all()


@app.post("/api/v1/groups", response_model=GroupOut)
def create_group(payload: GroupCreate, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    if db.query(Group.id).filter_by(name=payload.name).first():
        raise HTTPException(409, {"code": "GROUP_EXISTS", "message": "Group name already exists.", "retryable": False})
    row = Group(name=payload.name, description=payload.description)
    db.add(row)
    record_audit(db, "group.create", user.id, "group", row.id, {"name": row.name})
    db.commit(); db.refresh(row)
    return row


@app.patch("/api/v1/groups/{group_id}", response_model=GroupOut)
def update_group(group_id: str, payload: GroupUpdate, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    row = db.get(Group, group_id)
    if not row:
        raise HTTPException(404, "Group not found")
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] != row.name and db.query(Group.id).filter_by(name=changes["name"]).first():
        raise HTTPException(409, {"code": "GROUP_EXISTS", "message": "Group name already exists.", "retryable": False})
    for key, value in changes.items():
        setattr(row, key, value)
    record_audit(db, "group.update", user.id, "group", row.id, {"fields": sorted(changes)})
    db.commit(); db.refresh(row)
    return row


@app.delete("/api/v1/groups/{group_id}")
def delete_group(group_id: str, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    row = db.get(Group, group_id)
    if not row:
        raise HTTPException(404, "Group not found")
    if db.query(User.id).filter_by(group_id=group_id).first():
        raise HTTPException(409, {"code": "GROUP_NOT_EMPTY", "message": "Move all members out of this group before deleting it.", "retryable": False})
    if kb_ids_visible_to_group(db, group_id):
        raise HTTPException(409, {"code": "GROUP_NOT_EMPTY", "message": "Group still owns Knowledge Bases; reassign them first.", "retryable": False})
    record_audit(db, "group.delete", user.id, "group", row.id, {"name": row.name})
    db.commit()
    db.delete(row)
    db.commit()
    return {"status": "deleted", "group_id": group_id}


@app.post("/api/v1/knowledge-bases", response_model=KnowledgeBaseOut)
def create_kb(payload: KnowledgeBaseCreate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    requested_code = payload.code
    if requested_code:
        existing = db.query(KnowledgeBase).filter_by(code=requested_code).first()
        if existing:
            raise HTTPException(409, {"code": "KNOWLEDGE_BASE_CODE_EXISTS",
                                      "message": f"Knowledge Base code '{requested_code}' already exists. Choose another code.",
                                      "retryable": False, "existing_status": existing.status,
                                      "existing_deleted": bool(existing.deleted_at)})
        code = requested_code
    else:
        # Keep human-readable slugs for Latin names.  A stable hash fallback
        # prevents every Thai/non-Latin name from collapsing to
        # "knowledge-base".  Suffixes keep codes unique across soft-deleted
        # records and repeated names.
        normalized = unicodedata.normalize("NFKD", payload.name).encode("ascii", "ignore").decode("ascii").lower()
        base = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
        if len(base) < 2:
            base = f"kb-{hashlib.sha256(payload.name.strip().encode('utf-8')).hexdigest()[:10]}"
        base = base[:120].rstrip("-")
        code, suffix = base, 2
        while db.query(KnowledgeBase.id).filter_by(code=code).first():
            suffix_text = f"-{suffix}"
            code = f"{base[:120 - len(suffix_text)].rstrip('-')}{suffix_text}"
            suffix += 1
    values = payload.model_dump(exclude={"code"})
    kb = KnowledgeBase(**values, code=code, retrieval_config=DEFAULT_RETRIEVAL_CONFIG.copy())
    db.add(kb); db.flush()
    # The creator owns the KB.  v1 writes a single owner row; the table is
    # already many-to-many so sharing later is an INSERT, not a migration.
    db.add(KbOwner(kb_id=kb.id, user_id=user.id))
    record_audit(db, "knowledge_base.create", user.id, "knowledge_base", kb.id, {"code": kb.code}); db.commit(); db.refresh(kb); return kb


@app.get("/api/v1/knowledge-bases", response_model=list[KnowledgeBaseOut])
def list_kbs(user: User = Depends(current_admin), db: Session = Depends(get_db)):
    visible = kb_ids_visible_to(db, user)
    return db.query(KnowledgeBase).filter(KnowledgeBase.deleted_at.is_(None), KnowledgeBase.id.in_(visible)).all()


@app.patch("/api/v1/knowledge-bases/{kb_id}/retrieval-config", response_model=KnowledgeBaseOut)
def update_retrieval_config(kb_id: str, payload: RetrievalConfigUpdate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    kb = db.get(KnowledgeBase, kb_id)
    if not kb or kb.deleted_at:
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    try:
        kb.retrieval_config = payload.merged(kb.retrieval_config or {})
    except ValueError as exc:
        raise HTTPException(422, {"code": "RETRIEVAL_CONFIG_INVALID", "message": str(exc), "retryable": False}) from exc
    record_audit(db, "knowledge_base.retrieval_config.update", user.id, "knowledge_base", kb.id,
                 {"config": kb.retrieval_config})
    db.commit(); db.refresh(kb)
    return kb


@app.patch("/api/v1/knowledge-bases/{kb_id}/icon", response_model=KnowledgeBaseOut)
def update_kb_icon(kb_id: str, payload: KnowledgeBaseIconUpdate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    kb = db.get(KnowledgeBase, kb_id)
    if not kb or kb.deleted_at:
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    kb.icon = payload.icon
    record_audit(db, "knowledge_base.icon.update", user.id, "knowledge_base", kb.id, {"icon": kb.icon})
    db.commit(); db.refresh(kb)
    return kb


@app.post("/api/v1/knowledge-bases/{kb_id}/activate")
def activate_kb(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    kb = db.get(KnowledgeBase, kb_id)
    if not kb or kb.deleted_at: raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    kb.status = "active"; record_audit(db, "knowledge_base.activate", user.id, "knowledge_base", kb.id); db.commit(); return {"status": "success"}


@app.post("/api/v1/knowledge-bases/{kb_id}/disable")
def disable_kb(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    kb = db.get(KnowledgeBase, kb_id)
    if not kb or kb.deleted_at:
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    kb.status = "disabled"
    record_audit(db, "knowledge_base.disable", user.id, "knowledge_base", kb.id)
    db.commit()
    return {"status": "success"}


@app.delete("/api/v1/knowledge-bases/{kb_id}")
def delete_kb(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    kb = db.get(KnowledgeBase, kb_id)
    if not kb or kb.deleted_at:
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    if db.query(Document.id).filter_by(knowledge_base_id=kb.id).filter(Document.deleted_at.is_(None)).first():
        raise HTTPException(409, {"code": "KNOWLEDGE_BASE_NOT_EMPTY", "message": "Delete or move all documents before deleting this Knowledge Base.", "retryable": False})
    kb.deleted_at, kb.status = datetime.utcnow(), "deleted"
    # Tokens scoped (either axis) to a deleted KB are revoked immediately —
    # leaving them active would grant access to a KB that no longer resolves.
    revoked = 0
    for token in db.query(TokenKey).filter(TokenKey.status == "active").all():
        scopes_kb = kb.id in (token.allowed_knowledge_base_ids or []) or token.allowed_ingest_knowledge_base_id == kb.id
        if scopes_kb:
            token.status, token.revoked_at = "revoked", datetime.utcnow()
            revoked += 1
    record_audit(db, "knowledge_base.delete", user.id, "knowledge_base", kb.id, {"revoked_tokens": revoked})
    db.commit()
    return {"status": "deleted", "knowledge_base_id": kb.id, "revoked_tokens": revoked}


@app.get("/api/v1/knowledge-bases/{kb_id}/document-templates", response_model=list[DocumentMetadataTemplateOut])
def get_document_templates(kb_id: str, include_inactive: bool = False, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    return list_templates(db, kb_id, include_inactive=include_inactive)


@app.post("/api/v1/knowledge-bases/{kb_id}/document-templates", response_model=DocumentMetadataTemplateOut)
def create_document_template(kb_id: str, payload: DocumentMetadataTemplateCreate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    code = payload.code or template_code(payload.name)
    if code.casefold() in SYSTEM_TEMPLATE_CODES or db.query(DocumentMetadataTemplate.id).filter(
        DocumentMetadataTemplate.knowledge_base_id == kb_id,
        func.lower(DocumentMetadataTemplate.code) == code.casefold(),
    ).first():
        raise HTTPException(409, {"code": "DOCUMENT_TEMPLATE_CODE_EXISTS", "message": "A document type with this code already exists.", "retryable": False})
    if payload.name.casefold() in SYSTEM_TEMPLATE_NAMES or any(row.name.casefold() == payload.name.casefold() for row in db.query(DocumentMetadataTemplate).filter_by(knowledge_base_id=kb_id).all()):
        raise HTTPException(409, {"code": "DOCUMENT_TEMPLATE_NAME_EXISTS", "message": "A document type with this name already exists.", "retryable": False})
    custom_fields = [item.model_dump() for item in payload.fields]
    row = DocumentMetadataTemplate(knowledge_base_id=kb_id, code=code, name=payload.name, description=payload.description,
                                   base_document_type=payload.base_document_type, custom_fields=custom_fields,
                                   fields=merge_profile_fields(payload.base_document_type, custom_fields))
    db.add(row)
    record_audit(db, "document_template.create", user.id, "document_template", row.id, {"knowledge_base_id": kb_id, "code": code, "base_document_type": row.base_document_type})
    db.commit(); db.refresh(row)
    usage_count = db.query(func.count(Document.id)).filter(Document.metadata_template_id == row.id, Document.deleted_at.is_(None)).scalar() or 0
    return {"id": row.id, "code": row.code, "name": row.name, "description": row.description, "base_document_type": row.base_document_type,
            "fields": merge_profile_fields(row.base_document_type, custom_template_fields(row)), "version": row.version, "is_active": row.is_active, "is_system": False, "usage_count": usage_count}


@app.patch("/api/v1/document-templates/{template_id}", response_model=DocumentMetadataTemplateOut)
def update_document_template(template_id: str, payload: DocumentMetadataTemplateUpdate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    row = db.get(DocumentMetadataTemplate, template_id)
    if not row:
        raise HTTPException(404, "Document template not found")
    values = payload.model_dump(exclude_unset=True)
    if "name" in values and (values["name"].casefold() in SYSTEM_TEMPLATE_NAMES or any(
        other.id != row.id and other.name.casefold() == values["name"].casefold()
        for other in db.query(DocumentMetadataTemplate).filter_by(knowledge_base_id=row.knowledge_base_id).all()
    )):
        raise HTTPException(409, {"code": "DOCUMENT_TEMPLATE_NAME_EXISTS", "message": "A document type with this name already exists.", "retryable": False})
    if "fields" in values:
        raw_fields = [item.model_dump() for item in values["fields"]]
        values["custom_fields"] = raw_fields
        values["fields"] = merge_profile_fields(values.get("base_document_type", row.base_document_type), raw_fields)
    elif "base_document_type" in values:
        raw_fields = custom_template_fields(row)
        values["custom_fields"] = raw_fields
        values["fields"] = merge_profile_fields(values["base_document_type"], raw_fields)
    for key, value in values.items():
        setattr(row, key, value)
    if "fields" in values or "base_document_type" in values:
        row.version += 1
    record_audit(db, "document_template.update", user.id, "document_template", row.id, {"fields": sorted(values), "version": row.version})
    db.commit(); db.refresh(row)
    usage_count = db.query(func.count(Document.id)).filter(Document.metadata_template_id == row.id, Document.deleted_at.is_(None)).scalar() or 0
    return {"id": row.id, "code": row.code, "name": row.name, "description": row.description, "base_document_type": row.base_document_type,
            "fields": merge_profile_fields(row.base_document_type, custom_template_fields(row)), "version": row.version, "is_active": row.is_active, "is_system": False, "usage_count": usage_count}


@app.delete("/api/v1/document-templates/{template_id}")
def deactivate_document_template(template_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    row = db.get(DocumentMetadataTemplate, template_id)
    if not row:
        raise HTTPException(404, "Document template not found")
    row.is_active = False
    record_audit(db, "document_template.deactivate", user.id, "document_template", row.id)
    db.commit()
    return {"status": "inactive", "template_id": row.id}


@app.post("/api/v1/document-templates/{template_id}/activate")
def activate_document_template(template_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    row = db.get(DocumentMetadataTemplate, template_id)
    if not row:
        raise HTTPException(404, "Document template not found")
    row.is_active = True
    record_audit(db, "document_template.activate", user.id, "document_template", row.id)
    db.commit()
    return {"status": "active", "template_id": row.id}


def _upload_metadata(template_id: str | None, document_type: str, metadata_json: str | None, db: Session, kb_id: str) -> tuple[dict, str, dict]:
    try:
        values = json.loads(metadata_json) if metadata_json else {}
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("DOCUMENT_METADATA_INVALID") from exc
    template = resolve_template(db, kb_id, template_id, document_type)
    return template, template["base_document_type"], validate_metadata_values(template.get("fields") or [], values)


@app.post("/api/v1/knowledge-bases/{kb_id}/documents")
def upload_document(kb_id: str, file: UploadFile = File(...), title: str | None = Form(None), document_type: str = Form("general"), template_id: str | None = Form(None), metadata_json: str | None = Form(None), published_at: date | None = Form(None), user: User = Depends(current_admin), db: Session = Depends(get_db)):
    kb = db.get(KnowledgeBase, kb_id)
    if not kb or kb.deleted_at: raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    if kb.status == "disabled":
        raise HTTPException(409, {"code": "KNOWLEDGE_BASE_DISABLED", "message": "Activate this Knowledge Base before uploading documents.", "retryable": False})
    try:
        template, profile, metadata = _upload_metadata(template_id, document_type, metadata_json, db, kb_id)
        doc, job = create_document_job(db, kb_id, file, title, profile, published_at, template, metadata)
    except ValueError as exc:
        status_code = 413 if str(exc) == "FILE_TOO_LARGE" else 400
        raise HTTPException(status_code, {"code": str(exc), "message": "Upload rejected", "retryable": False})
    record_audit(db, "document.upload", user.id, "document", doc.id, {"knowledge_base_id": kb_id, "filename": doc.original_filename, "document_type": doc.document_type}); db.commit()
    return {"status": "queued", "document_id": doc.id, "job_id": job.id, "document_type": doc.document_type, "template_id": doc.metadata_template_id, "legal_extraction_automatic": doc.document_type in {"legal", "regulation", "contract"}}


@app.post("/api/v1/knowledge-bases/{kb_id}/documents/batch")
def upload_documents_batch(kb_id: str, files: list[UploadFile] = File(...), title: str | None = Form(None), document_type: str = Form("general"), template_id: str | None = Form(None), metadata_json: str | None = Form(None), user: User = Depends(current_admin), db: Session = Depends(get_db)):
    """Queue a bounded batch while keeping each file an independent job.

    A validation, duplicate, or storage failure is isolated to its file so a
    large batch can continue and the UI can retry only the failed documents.
    The document type applies uniformly to the whole batch; titles are only
    accepted for a single-file batch to avoid misleading shared titles.
    """
    kb = db.get(KnowledgeBase, kb_id)
    if not kb or kb.deleted_at:
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    if kb.status == "disabled":
        raise HTTPException(409, {"code": "KNOWLEDGE_BASE_DISABLED", "message": "Activate this Knowledge Base before uploading documents.", "retryable": False})
    if not files:
        raise HTTPException(400, {"code": "BATCH_FILES_REQUIRED", "message": "Select at least one file.", "retryable": False})
    if len(files) > 20:
        raise HTTPException(400, {"code": "BATCH_TOO_MANY_FILES", "message": "A batch can contain at most 20 files.", "retryable": False})
    try:
        template, profile, metadata = _upload_metadata(template_id, document_type, metadata_json, db, kb_id)
    except ValueError as exc:
        raise HTTPException(400, {"code": str(exc), "message": "Document metadata is invalid.", "retryable": False})

    results = []
    for upload in files:
        filename = upload.filename or "unnamed-file"
        result = {"filename": filename, "status": "failed", "document_type": profile, "template_id": template.get("id")}
        try:
            doc, job = create_document_job(db, kb_id, upload, title if len(files) == 1 else None, profile, None, template, metadata)
            record_audit(db, "document.upload", user.id, "document", doc.id, {"knowledge_base_id": kb_id, "filename": doc.original_filename, "document_type": doc.document_type, "batch": True})
            db.commit()
            result.update({"status": "queued", "document_id": doc.id, "job_id": job.id, "legal_extraction_automatic": profile in {"legal", "regulation", "contract"}})
        except ValueError as exc:
            result.update({"error_code": str(exc), "message": "Upload rejected"})
        except Exception:
            db.rollback()
            result.update({"error_code": "UPLOAD_FAILED", "message": "Upload could not be queued"})
        results.append(result)

    queued_count = sum(item["status"] == "queued" for item in results)
    failed_count = len(results) - queued_count
    return {"status": "queued" if failed_count == 0 else "partial", "document_type": profile, "template_id": template.get("id"),
            "total": len(results), "queued_count": queued_count, "failed_count": failed_count, "results": results}


@app.get("/api/v1/knowledge-bases/{kb_id}/documents/page", response_model=DocumentPageOut)
def page_documents(kb_id: str, include_deleted: bool = False, limit: int = 50, offset: int = 0,
                   search: str | None = None, status: str | None = None, document_type: str | None = None,
                   template_id: str | None = None,
                   user: User = Depends(current_admin), db: Session = Depends(get_db)):
    """Return a bounded Documents-page slice without changing the legacy list contract."""
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    if limit < 1 or limit > 100 or offset < 0:
        raise HTTPException(400, {"code": "DOCUMENT_PAGE_INVALID", "message": "limit must be 1-100 and offset must be non-negative.", "retryable": False})
    rows = db.query(Document).filter(Document.knowledge_base_id == kb_id)
    if not include_deleted:
        rows = rows.filter(Document.deleted_at.is_(None))
    if search and search.strip():
        term = f"%{search.strip()}%"
        rows = rows.filter(or_(Document.title.ilike(term), Document.original_filename.ilike(term)))
    if status:
        rows = rows.filter(Document.status == status)
    if document_type:
        rows = rows.filter(Document.document_type == document_type)
    if template_id:
        if template_id.startswith("system:"):
            profile = template_id.removeprefix("system:")
            if profile not in {"general", "legal", "regulation", "contract"}:
                raise HTTPException(400, {"code": "DOCUMENT_TEMPLATE_INVALID", "message": "Document type is invalid.", "retryable": False})
            rows = rows.filter(or_(Document.metadata_template_id == template_id,
                                   (Document.metadata_template_id.is_(None)) & (Document.document_type == profile)))
        else:
            template = db.get(DocumentMetadataTemplate, template_id)
            if not template or template.knowledge_base_id != kb_id:
                raise HTTPException(400, {"code": "DOCUMENT_TEMPLATE_INVALID", "message": "Document type is invalid.", "retryable": False})
            rows = rows.filter(Document.metadata_template_id == template_id)
    total = rows.count()
    documents = rows.order_by(Document.created_at.desc()).offset(offset).limit(limit).all()
    has_legal_documents = db.query(Document.id).filter(
        Document.knowledge_base_id == kb_id,
        Document.document_type.in_(["legal", "regulation", "contract"]),
        Document.deleted_at.is_(None),
    ).first() is not None
    has_completed_documents = db.query(Document.id).filter(
        Document.knowledge_base_id == kb_id,
        Document.status == "completed",
        Document.deleted_at.is_(None),
    ).first() is not None
    processing_count = db.query(ProcessingJob.document_id).join(
        Document, Document.id == ProcessingJob.document_id
    ).filter(
        Document.knowledge_base_id == kb_id,
        Document.deleted_at.is_(None),
        ProcessingJob.status.in_(["queued", "running"]),
    ).distinct().count()
    document_ids = [document.id for document in documents]
    latest_jobs = {}
    if document_ids:
        jobs = db.query(ProcessingJob).filter(ProcessingJob.document_id.in_(document_ids)).order_by(ProcessingJob.created_at.desc()).all()
        for job in jobs:
            if job.document_id and job.document_id not in latest_jobs:
                latest_jobs[job.document_id] = job
    items = []
    for document in documents:
        item = DocumentOut.model_validate(document).model_dump()
        job = latest_jobs.get(document.id)
        if job:
            item.update(processing_job_status=job.status, processing_job_type=job.job_type,
                        processing_job_stage=job.current_stage, processing_job_progress_percent=job.progress_percent)
        items.append(item)
    return {"items": items, "total": total, "limit": limit, "offset": offset,
            "has_legal_documents": has_legal_documents,
            "has_completed_documents": has_completed_documents,
            "processing_count": processing_count}


@app.get("/api/v1/knowledge-bases/{kb_id}/documents", response_model=list[DocumentOut])
def list_documents(kb_id: str, include_deleted: bool = False, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    assert_kb_access(db, user, kb_id)
    rows = db.query(Document).filter_by(knowledge_base_id=kb_id)
    if not include_deleted:
        rows = rows.filter(Document.deleted_at.is_(None))
    documents = rows.order_by(Document.created_at.desc()).limit(200).all()
    document_ids = [document.id for document in documents]
    latest_jobs = {}
    if document_ids:
        jobs = db.query(ProcessingJob).filter(ProcessingJob.document_id.in_(document_ids)).order_by(ProcessingJob.created_at.desc()).all()
        for job in jobs:
            if job.document_id and job.document_id not in latest_jobs:
                # The query is newest-first; keep the first row per document.
                latest_jobs[job.document_id] = job
    result = []
    for document in documents:
        item = DocumentOut.model_validate(document).model_dump()
        job = latest_jobs.get(document.id)
        if job:
            item.update(processing_job_status=job.status, processing_job_type=job.job_type,
                        processing_job_stage=job.current_stage, processing_job_progress_percent=job.progress_percent)
        result.append(item)
    return result


@app.post("/api/v1/knowledge-bases/{kb_id}/documents/reindex")
def reindex_embeddings(kb_id: str, force: bool = False, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    count = queue_embedding_reindex(db, kb_id, force)
    record_audit(db, "document.embedding_reindex", user.id, "knowledge_base", kb_id, {"count": count, "force": force}); db.commit()
    return {"status": "queued", "count": count}


@app.get("/api/v1/documents/{document_id}/text")
def document_text(document_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if not doc: raise HTTPException(404, "Document not found")
    assert_kb_access(db, user, doc.knowledge_base_id)
    return {"document_id": doc.id, "status": doc.status, "document_type": doc.document_type, "metadata_template_id": doc.metadata_template_id,
            "metadata_template_name": doc.metadata_template_name, "metadata_template_version": doc.metadata_template_version,
            "metadata_template_fields": doc.metadata_template_fields or [],
            "document_metadata": doc.document_metadata or {}, "text": doc.extracted_text, "error_code": doc.error_code, "legal_metadata": doc.legal_metadata}


@app.get("/api/v1/documents/{document_id}/jobs")
def document_jobs(document_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    assert_kb_access(db, user, doc.knowledge_base_id)
    rows = db.query(ProcessingJob).filter_by(document_id=document_id).order_by(ProcessingJob.created_at.desc()).all()
    return [{"id": job.id, "type": job.job_type, "status": job.status, "stage": job.current_stage, "progress_percent": job.progress_percent,
             "attempt_count": job.attempt_count, "error_code": job.error_code, "error_message": job.error_message} for job in rows]


@app.post("/api/v1/documents/{document_id}/legal-extract")
def extract_legal_metadata(document_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if not doc or doc.deleted_at:
        raise HTTPException(404, "Document not found")
    assert_kb_access(db, user, doc.knowledge_base_id)
    if doc.status != "completed" or not doc.extracted_text:
        raise HTTPException(409, {"code": "DOCUMENT_NOT_READY", "message": "Process the document before legal extraction.", "retryable": False})
    active = db.query(ProcessingJob.id).filter(ProcessingJob.document_id == doc.id, ProcessingJob.job_type == "EXTRACT_LEGAL_METADATA", ProcessingJob.status.in_(["queued", "running"])).first()
    if active:
        return {"status": "queued", "document_id": doc.id, "job_id": active[0]}
    job = ProcessingJob(document_id=doc.id, knowledge_base_id=doc.knowledge_base_id, job_type="EXTRACT_LEGAL_METADATA")
    db.add(job)
    record_audit(db, "document.legal_metadata.extract", user.id, "document", doc.id)
    db.commit(); db.refresh(job)
    return {"status": "queued", "document_id": doc.id, "job_id": job.id}


@app.patch("/api/v1/documents/{document_id}/legal-metadata")
def update_legal_metadata(document_id: str, payload: LegalMetadataUpdate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if doc: assert_kb_access(db, user, doc.knowledge_base_id)
    if not doc or doc.deleted_at:
        raise HTTPException(404, "Document not found")
    current = doc.legal_metadata or {}
    current.update(payload.metadata)
    doc.legal_metadata = current
    record_audit(db, "document.legal_metadata.update", user.id, "document", doc.id, {"fields": sorted(payload.metadata.keys())})
    db.commit()
    return {"status": "updated", "document_id": doc.id, "legal_metadata": doc.legal_metadata}


@app.patch("/api/v1/documents/{document_id}/metadata")
def update_document_metadata(document_id: str, payload: DocumentMetadataUpdate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if doc: assert_kb_access(db, user, doc.knowledge_base_id)
    if not doc or doc.deleted_at:
        raise HTTPException(404, "Document not found")
    if "published_at" in payload.model_fields_set:
        doc.published_at = payload.published_at
    if payload.values is not None:
        try:
            # Keep existing documents editable even after an administrator retires
            # their custom template. The field snapshot is part of the document's
            # provenance; resolving the live template is only needed for legacy
            # documents created before snapshots were introduced.
            fields = doc.metadata_template_fields or []
            if not fields and doc.metadata_template_name is None:
                template = resolve_template(db, doc.knowledge_base_id, doc.metadata_template_id, doc.document_type)
                fields = template.get("fields") or []
            doc.document_metadata = validate_metadata_values(fields, payload.values)
            doc.metadata_search_text = metadata_search_text(fields, doc.document_metadata)
            sync_document_metadata_values(db, doc)
            sync_document_metadata_graph(db, doc)
        except ValueError as exc:
            raise HTTPException(400, {"code": str(exc), "message": "Document metadata is invalid.", "retryable": False})
    record_audit(db, "document.metadata.update", user.id, "document", doc.id, {"published_at": str(doc.published_at) if doc.published_at else None,
                 "metadata_fields": sorted((doc.document_metadata or {}).keys())})
    db.commit()
    return {"status": "updated", "document_id": doc.id, "published_at": doc.published_at, "document_metadata": doc.document_metadata or {}}


@app.put("/api/v1/documents/{document_id}/legal-metadata")
def replace_legal_metadata(document_id: str, payload: LegalMetadataUpdate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if not doc or doc.deleted_at:
        raise HTTPException(404, "Document not found")
    doc.legal_metadata = payload.metadata
    record_audit(db, "document.legal_metadata.replace", user.id, "document", doc.id)
    db.commit()
    return {"status": "replaced", "document_id": doc.id, "legal_metadata": doc.legal_metadata}


@app.delete("/api/v1/documents/{document_id}/legal-metadata")
def delete_legal_metadata(document_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if doc: assert_kb_access(db, user, doc.knowledge_base_id)
    if not doc or doc.deleted_at:
        raise HTTPException(404, "Document not found")
    doc.legal_metadata = None
    record_audit(db, "document.legal_metadata.delete", user.id, "document", doc.id)
    db.commit()
    return {"status": "deleted", "document_id": doc.id}


@app.post("/api/v1/documents/{document_id}/reprocess")
def reprocess_document(document_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if doc: assert_kb_access(db, user, doc.knowledge_base_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    active = db.query(ProcessingJob).filter(
        ProcessingJob.document_id == doc.id,
        ProcessingJob.status.in_(["queued", "running"]),
    ).order_by(ProcessingJob.created_at.desc()).first()
    if active:
        raise HTTPException(409, {
            "code": "DOCUMENT_PROCESSING_IN_PROGRESS",
            "message": "This document already has a processing job in progress.",
            "retryable": True,
            "job_id": active.id,
        })
    doc.status, doc.error_code, doc.error_message = "queued", None, None
    job = ProcessingJob(document_id=doc.id, knowledge_base_id=doc.knowledge_base_id, job_type="REPROCESS_DOCUMENT")
    db.add(job); record_audit(db, "document.reprocess", user.id, "document", doc.id)
    db.commit()
    return {"status": "queued", "document_id": doc.id, "job_id": job.id}


@app.delete("/api/v1/documents/{document_id}")
def delete_document(document_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if doc: assert_kb_access(db, user, doc.knowledge_base_id)
    if not doc or doc.deleted_at:
        raise HTTPException(404, "Document not found")
    doc.deleted_at, doc.status = datetime.utcnow(), "deleted"
    db.query(ProcessingJob).filter(ProcessingJob.document_id == doc.id, ProcessingJob.status.in_(["queued", "running"])).update({"status": "cancelled"}, synchronize_session=False)
    record_audit(db, "document.delete", user.id, "document", doc.id, {"knowledge_base_id": doc.knowledge_base_id})
    db.commit()
    return {"status": "deleted", "document_id": doc.id}


@app.post("/api/v1/documents/{document_id}/restore")
def restore_document(document_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if doc: assert_kb_access(db, user, doc.knowledge_base_id)
    if not doc or not doc.deleted_at:
        raise HTTPException(404, "Deleted document not found")
    doc.deleted_at, doc.status = None, "queued"
    job = ProcessingJob(document_id=doc.id, knowledge_base_id=doc.knowledge_base_id, job_type="RESTORE_DOCUMENT")
    db.add(job); record_audit(db, "document.restore", user.id, "document", doc.id)
    db.commit()
    return {"status": "queued", "document_id": doc.id, "job_id": job.id}


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    job = db.get(ProcessingJob, job_id)
    if not job: raise HTTPException(404, "Job not found")
    return {"id": job.id, "type": job.job_type, "status": job.status, "stage": job.current_stage, "progress_percent": job.progress_percent,
            "attempt_count": job.attempt_count, "error_code": job.error_code, "error_message": job.error_message}


@app.post("/api/v1/internal/process-next")
def process_one(_: User = Depends(current_admin), db: Session = Depends(get_db)):
    return {"processed": process_next_job(db)}


@app.post("/api/v1/knowledge-bases/{kb_id}/entities", response_model=EntityOut)
def add_entity(kb_id: str, payload: EntityCreate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    if payload.document_id:
        document = db.get(Document, payload.document_id)
        if not document or document.knowledge_base_id != kb_id:
            raise HTTPException(400, "Document does not belong to the knowledge base")
    entity = create_entity(db, kb_id, payload)
    record_audit(db, "entity.create", user.id, "entity", entity.id, {"knowledge_base_id": kb_id}); db.commit()
    return entity


@app.get("/api/v1/knowledge-bases/{kb_id}/entities", response_model=list[EntityOut])
def list_entities(kb_id: str, search: str = "", user: User = Depends(current_admin), db: Session = Depends(get_db)):
    assert_kb_access(db, user, kb_id)
    rows = db.query(Entity).filter_by(knowledge_base_id=kb_id).filter(Entity.deleted_at.is_(None))
    if search:
        rows = rows.filter(Entity.name.ilike(f"%{search}%"))
    return rows.order_by(Entity.name).limit(100).all()


@app.patch("/api/v1/entities/{entity_id}", response_model=EntityOut)
def update_entity(entity_id: str, payload: EntityUpdate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    entity = db.get(Entity, entity_id)
    if entity: assert_kb_access(db, user, entity.knowledge_base_id)
    if not entity or entity.deleted_at:
        raise HTTPException(404, "Entity not found")
    values = payload.model_dump(exclude_unset=True)
    if "name" in values:
        entity.name, entity.canonical_name = values["name"], " ".join(values["name"].casefold().split())
    for key, value in values.items():
        if key != "name": setattr(entity, key, value)
    db.add(GraphProjectionEvent(event_type="entity", entity_id=entity.id)); record_audit(db, "entity.update", user.id, "entity", entity.id, values); db.commit(); db.refresh(entity)
    return entity


@app.delete("/api/v1/entities/{entity_id}")
def delete_entity(entity_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    entity = db.get(Entity, entity_id)
    if entity: assert_kb_access(db, user, entity.knowledge_base_id)
    if not entity or entity.deleted_at:
        raise HTTPException(404, "Entity not found")
    entity.deleted_at = datetime.utcnow()
    db.query(Relationship).filter((Relationship.source_entity_id == entity.id) | (Relationship.target_entity_id == entity.id), Relationship.deleted_at.is_(None)).update({"deleted_at": datetime.utcnow()}, synchronize_session=False)
    record_audit(db, "entity.delete", user.id, "entity", entity.id); db.commit()
    return {"status": "deleted", "entity_id": entity.id}


@app.post("/api/v1/knowledge-bases/{kb_id}/relationships", response_model=RelationshipOut)
def add_relationship(kb_id: str, payload: RelationshipCreate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    assert_kb_access(db, user, kb_id)
    try:
        relationship = create_relationship(db, kb_id, payload)
        record_audit(db, "relationship.create", user.id, "relationship", relationship.id, {"knowledge_base_id": kb_id}); db.commit()
        return relationship
    except ValueError:
        raise HTTPException(400, "Entities must exist in the selected knowledge base")


@app.get("/api/v1/knowledge-bases/{kb_id}/relationships", response_model=list[RelationshipOut])
def list_relationships(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    assert_kb_access(db, user, kb_id)
    return db.query(Relationship).filter_by(knowledge_base_id=kb_id).filter(Relationship.deleted_at.is_(None)).limit(200).all()


@app.get("/api/v1/knowledge-bases/{kb_id}/legal-graph")
def get_legal_graph(kb_id: str, view: str = "verified", user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if view not in {"verified", "suggested", "manual", "all"}:
        raise HTTPException(400, {"code": "LEGAL_GRAPH_VIEW_INVALID", "message": "view must be verified, suggested, manual, or all", "retryable": False})
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    status_filter = {"verified": ["verified"], "suggested": ["suggested"], "all": ["verified", "suggested", "rejected"]}
    relationship_query = db.query(Relationship).filter(Relationship.knowledge_base_id == kb_id, Relationship.deleted_at.is_(None))
    if view == "manual":
        relationship_query = relationship_query.filter(Relationship.origin == "manual")
    elif view == "all":
        relationship_query = relationship_query.filter((Relationship.is_legal.is_(True)) | (Relationship.origin == "manual"))
    else:
        relationship_query = relationship_query.filter(Relationship.is_legal.is_(True), Relationship.review_status.in_(status_filter[view]))
    relationships = relationship_query.limit(500).all()
    node_ids = {edge.source_entity_id for edge in relationships} | {edge.target_entity_id for edge in relationships}
    entity_query = db.query(Entity).filter(Entity.knowledge_base_id == kb_id, Entity.deleted_at.is_(None))
    if view == "manual":
        entity_query = entity_query.filter(Entity.origin == "manual")
    elif view == "all":
        entity_query = entity_query.filter((Entity.is_legal.is_(True)) | (Entity.origin == "manual"))
    else:
        entity_query = entity_query.filter(Entity.is_legal.is_(True))
    if view == "verified":
        entity_query = entity_query.filter(Entity.review_status == "verified")
    elif node_ids:
        entity_query = entity_query.filter((Entity.review_status == "verified") | Entity.id.in_(node_ids))
    else:
        entity_query = entity_query.filter(Entity.review_status == "suggested")
    entities = entity_query.limit(500).all()
    source_rows = db.query(RelationshipSource, Document).join(Document, Document.id == RelationshipSource.document_id).filter(
        RelationshipSource.relationship_id.in_([edge.id for edge in relationships]) if relationships else False
    ).all()
    sources: dict[str, list[dict]] = {}
    for source, document in source_rows:
        sources.setdefault(source.relationship_id, []).append({"document_id": document.id, "title": document.title or document.original_filename, "excerpt": source.excerpt})
    return {
        "knowledge_base_id": kb_id, "view": view,
        "nodes": [EntityOut.model_validate(entity).model_dump() for entity in entities],
        "edges": [{**RelationshipOut.model_validate(edge).model_dump(), "sources": sources.get(edge.id, [])} for edge in relationships],
    }


@app.get("/api/v1/knowledge-bases/{kb_id}/legal-map")
def get_legal_map(kb_id: str, view: str = "verified", instrument_id: str | None = None,
                  max_nodes: int = 80, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    """Return a bounded, document-scoped legal graph projection for progressive disclosure.

    The full legal graph remains available through ``legal-graph`` for advanced users,
    while this endpoint is the human-facing overview: one card per legal instrument,
    followed by an explicitly selected instrument structure.
    """
    assert_kb_access(db, user, kb_id)
    if view not in {"verified", "suggested", "all"}:
        raise HTTPException(400, {"code": "LEGAL_GRAPH_VIEW_INVALID", "message": "view must be verified, suggested, or all", "retryable": False})
    if max_nodes < 10 or max_nodes > 200:
        raise HTTPException(400, {"code": "LEGAL_GRAPH_LIMIT_INVALID", "message": "max_nodes must be between 10 and 200", "retryable": False})
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")

    instruments = db.query(LegalInstrument).filter_by(knowledge_base_id=kb_id).order_by(
        LegalInstrument.family_id.asc(), LegalInstrument.version_date.asc(), LegalInstrument.created_at.asc()
    ).all()
    documents = {document.id: document for document in db.query(Document).filter(Document.id.in_([item.document_id for item in instruments])).all()} if instruments else {}
    family_rows: dict[str, list[LegalInstrument]] = {}
    for item in instruments:
        family_rows.setdefault(item.family_id or item.id, []).append(item)

    def entity_ids_for_document(document_id: str) -> set[str]:
        rows = db.query(EntitySource.entity_id).filter(EntitySource.document_id == document_id).all()
        return {row[0] for row in rows}

    def edge_query(entity_ids: set[str]):
        if not entity_ids:
            return []
        query = db.query(Relationship).filter(
            Relationship.knowledge_base_id == kb_id,
            Relationship.deleted_at.is_(None),
            Relationship.source_entity_id.in_(entity_ids),
            Relationship.target_entity_id.in_(entity_ids),
            Relationship.is_legal.is_(True),
        )
        if view == "verified":
            query = query.filter(Relationship.review_status == "verified")
        elif view == "suggested":
            query = query.filter(Relationship.review_status == "suggested")
        return query.limit(max_nodes * 3).all()

    summaries = []
    instrument_entity_ids: dict[str, set[str]] = {}
    for item in instruments:
        ids = entity_ids_for_document(item.document_id)
        instrument_entity_ids[item.id] = ids
        edges = edge_query(ids)
        document = documents.get(item.document_id)
        summaries.append({
            "id": item.id,
            "document_id": item.document_id,
            "title": item.official_title or (document.title if document else None) or (document.original_filename if document else "Untitled instrument"),
            "filename": document.original_filename if document else None,
            "kind": item.kind,
            "document_class": item.document_class,
            "status": item.status,
            "status_reason": item.status_reason,
            "review_status": item.review_status,
            "authority_level": item.authority_level,
            "source_uri": item.source_uri,
            "source_reference": item.source_reference,
            "family_id": item.family_id,
            "version_label": item.version_label,
            "version_date": item.version_date.isoformat() if item.version_date else None,
            "effective_from": item.effective_from.isoformat() if item.effective_from else None,
            "effective_to": item.effective_to.isoformat() if item.effective_to else None,
            "entity_count": len(ids),
            "relationship_count": len(edges),
        })

    cross_relations = db.query(LegalInstrumentRelation).filter(
        LegalInstrumentRelation.knowledge_base_id == kb_id,
        LegalInstrumentRelation.target_instrument_id.is_not(None),
    )
    if view == "verified":
        cross_relations = cross_relations.filter(LegalInstrumentRelation.review_status == "verified")
    elif view == "suggested":
        cross_relations = cross_relations.filter(LegalInstrumentRelation.review_status == "suggested")
    cross_edges = [{
        "id": relation.id,
        "source_instrument_id": relation.source_instrument_id,
        "target_instrument_id": relation.target_instrument_id,
        "relation": relation.relation,
        "review_status": relation.review_status,
        "origin": relation.origin,
        "confidence": relation.confidence,
        "evidence_quote": relation.evidence_quote,
    } for relation in cross_relations.limit(300).all()]

    # The editable relationship table is the canonical source for internal
    # edges. Cross-document suggestions may also exist only in the legal
    # registry when the target instrument has not been resolved yet. Count
    # those registry rows as well, while skipping rows already linked to a
    # materialized Relationship to avoid double-counting.
    relationship_rows = db.query(Relationship).filter(
        Relationship.knowledge_base_id == kb_id,
        Relationship.deleted_at.is_(None),
    ).all()
    legal_relationships = [edge for edge in relationship_rows if edge.is_legal]
    linked_relationship_ids = {edge.id for edge in legal_relationships}
    relationship_summary = {
        "verified": 0,
        "suggested": 0,
        "rejected": 0,
        "manual": 0,
        "internal": 0,
        "cross_document": 0,
    }

    def add_summary(review_status: str | None, origin: str | None, bucket: str) -> None:
        if review_status in {"verified", "suggested", "rejected"}:
            relationship_summary[review_status] += 1
        if origin == "manual":
            relationship_summary["manual"] += 1
        relationship_summary[bucket] += 1

    for edge in legal_relationships:
        add_summary(edge.review_status, edge.origin, "internal")
    relationship_summary["manual"] += sum(1 for edge in relationship_rows if edge.origin == "manual" and not edge.is_legal)

    registry_relations = db.query(LegalInstrumentRelation).filter(
        LegalInstrumentRelation.knowledge_base_id == kb_id,
    ).all()
    for relation in registry_relations:
        if relation.relationship_id in linked_relationship_ids:
            continue
        add_summary(relation.review_status, relation.origin, "cross_document")

    result = {"knowledge_base_id": kb_id, "view": view, "mode": "map", "instruments": summaries,
              "families": [{"id": family_id, "title": next((row["title"] for row in summaries if row["family_id"] == family_id), "Legal family"),
                             "instrument_ids": [row.id for row in rows]} for family_id, rows in family_rows.items()],
              "cross_document_relations": cross_edges, "relationship_summary": relationship_summary}
    if not instrument_id:
        return result
    selected = next((item for item in instruments if item.id == instrument_id), None)
    if not selected:
        raise HTTPException(404, "Legal instrument not found")
    ids = instrument_entity_ids.get(selected.id, set())
    entities = db.query(Entity).filter(Entity.id.in_(list(ids)), Entity.deleted_at.is_(None)).order_by(Entity.entity_type.asc(), Entity.name.asc()).limit(max_nodes).all() if ids else []
    visible_ids = {entity.id for entity in entities}
    edges = [edge for edge in edge_query(visible_ids) if edge.source_entity_id in visible_ids and edge.target_entity_id in visible_ids]
    edge_sources: dict[str, list[dict]] = {}
    if edges:
        source_rows = db.query(RelationshipSource, Document).join(
            Document, Document.id == RelationshipSource.document_id
        ).filter(
            RelationshipSource.relationship_id.in_([edge.id for edge in edges]),
            Document.deleted_at.is_(None),
        ).all()
        for source, document in source_rows:
            edge_sources.setdefault(source.relationship_id, []).append({
                "document_id": document.id,
                "title": document.title or document.original_filename,
                "excerpt": source.excerpt,
            })
    result["mode"] = "instrument"
    result["instrument"] = next(item for item in summaries if item["id"] == selected.id)
    result["nodes"] = [EntityOut.model_validate(entity).model_dump() for entity in entities]
    result["edges"] = [{**RelationshipOut.model_validate(edge).model_dump(), "sources": edge_sources.get(edge.id, [])} for edge in edges]
    result["warnings"] = (["This instrument view is bounded to the selected document."] if len(ids) > max_nodes else [])
    return result


@app.post("/api/v1/knowledge-bases/{kb_id}/legal-graph/rebuild")
def queue_legal_graph_rebuild(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    active = db.query(ProcessingJob).filter(
        ProcessingJob.knowledge_base_id == kb_id, ProcessingJob.job_type == "REBUILD_LEGAL_GRAPH",
        ProcessingJob.status.in_(["queued", "running"]),
    ).first()
    if active:
        return {"status": active.status, "job_id": active.id, "knowledge_base_id": kb_id}
    job = ProcessingJob(knowledge_base_id=kb_id, job_type="REBUILD_LEGAL_GRAPH", current_stage="queued")
    db.add(job); db.flush()
    record_audit(db, "legal_graph.rebuild.queue", user.id, "knowledge_base", kb_id, {"job_id": job.id})
    db.commit()
    return {"status": "queued", "job_id": job.id, "knowledge_base_id": kb_id}


@app.get("/api/v1/knowledge-bases/{kb_id}/legal-graph/rebuild")
def legal_graph_rebuild_status(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    assert_kb_access(db, user, kb_id)
    job = db.query(ProcessingJob).filter_by(knowledge_base_id=kb_id, job_type="REBUILD_LEGAL_GRAPH").order_by(ProcessingJob.created_at.desc()).first()
    if not job:
        return {"status": "not_started", "job_id": None}
    return {"status": job.status, "job_id": job.id, "stage": job.current_stage, "progress_percent": job.progress_percent,
            "error_code": job.error_code, "error_message": job.error_message}


@app.patch("/api/v1/relationships/{relationship_id}/legal-review", response_model=RelationshipOut)
def review_legal_relationship(relationship_id: str, payload: LegalRelationshipReview, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    relationship = db.get(Relationship, relationship_id)
    if relationship: assert_kb_access(db, user, relationship.knowledge_base_id)
    if not relationship or relationship.deleted_at or not relationship.is_legal or relationship.origin != "ai_suggestion":
        raise HTTPException(404, "Suggested legal relationship not found")
    if relationship.review_status != "suggested":
        raise HTTPException(409, {"code": "LEGAL_RELATIONSHIP_ALREADY_REVIEWED", "message": "This legal suggestion has already been reviewed.", "retryable": False})
    relationship.review_status = payload.status
    relationship.attributes = {**(relationship.attributes or {}), "review_note": payload.note, "reviewed_by": user.username, "reviewed_at": datetime.utcnow().isoformat()}
    db.add(GraphProjectionEvent(event_type="relationship", relationship_id=relationship.id))
    sync_legal_instrument_relation_review(db, relationship)
    record_audit(db, f"legal_graph.relationship.{payload.status}", user.id, "relationship", relationship.id, {"note": payload.note})
    db.commit(); db.refresh(relationship)
    return relationship


@app.get("/api/v1/knowledge-bases/{kb_id}/legal-registry", response_model=list[LegalInstrumentOut])
def list_legal_registry(kb_id: str, status: str | None = None, kind: str | None = None, family_id: str | None = None,
                        document_id: str | None = None, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    rows = db.query(LegalInstrument).filter_by(knowledge_base_id=kb_id)
    if status:
        rows = rows.filter(LegalInstrument.status == status)
    if kind:
        rows = rows.filter(LegalInstrument.kind == kind)
    if family_id:
        rows = rows.filter(LegalInstrument.family_id == family_id)
    if document_id:
        rows = rows.filter(LegalInstrument.document_id == document_id)
    return rows.order_by(LegalInstrument.official_title).limit(500).all()


@app.get("/api/v1/legal-instruments/{instrument_id}")
def get_legal_instrument(instrument_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    instrument = db.get(LegalInstrument, instrument_id)
    if not instrument:
        raise HTTPException(404, "Legal instrument not found")
    assert_kb_access(db, user, instrument.knowledge_base_id)
    family_members = []
    if instrument.family_id:
        family_members = db.query(LegalInstrument).filter_by(
            family_id=instrument.family_id, knowledge_base_id=instrument.knowledge_base_id,
        ).order_by(LegalInstrument.effective_from).all()
    outgoing = db.query(LegalInstrumentRelation).filter_by(source_instrument_id=instrument.id).all()
    incoming = db.query(LegalInstrumentRelation).filter_by(target_instrument_id=instrument.id).all()
    def relation_out(row):
        return {"id": row.id, "relation": row.relation, "target_instrument_id": row.target_instrument_id,
                "target_text": row.target_text, "target_provision": row.target_provision,
                "review_status": row.review_status, "confidence": row.confidence}
    return {
        "instrument": LegalInstrumentOut.model_validate(instrument).model_dump(),
        "family": [LegalInstrumentOut.model_validate(row).model_dump() for row in family_members],
        "outgoing_relations": [relation_out(row) for row in outgoing],
        "incoming_relations": [relation_out(row) for row in incoming],
    }


@app.patch("/api/v1/legal-instruments/{instrument_id}", response_model=LegalInstrumentOut)
def update_legal_instrument(instrument_id: str, payload: LegalInstrumentUpdate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    instrument = db.get(LegalInstrument, instrument_id)
    if instrument: assert_kb_access(db, user, instrument.knowledge_base_id)
    if not instrument:
        raise HTTPException(404, "Legal instrument not found")
    if payload.family_id:
        family = db.get(LegalFamily, payload.family_id)
        if not family or family.knowledge_base_id != instrument.knowledge_base_id:
            raise HTTPException(400, "family_id does not exist in this Knowledge Base")
    fields = payload.model_dump(exclude_unset=True)
    for field, value in fields.items():
        setattr(instrument, field, value)
    # An admin decision is authoritative: the resolver never overwrites this row again.
    instrument.status_source, instrument.review_status = "manual", "verified"
    instrument.reviewed_at, instrument.reviewed_by = datetime.utcnow(), user.username
    if "status" in fields:
        instrument.status_reason = f"manual override by {user.username}"
    record_audit(db, "legal_instrument.update", user.id, "legal_instrument", instrument.id, {"fields": sorted(fields.keys())})
    db.commit(); db.refresh(instrument)
    return instrument


@app.get("/api/v1/knowledge-bases/{kb_id}/legal-registry/worklist")
def legal_registry_worklist(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    assert_kb_access(db, user, kb_id)
    """Return legal curation gaps instead of silently treating them as facts.

    The worklist is deliberately read-only and bounded.  It gives an operator
    the exact instruments/relations that need an authoritative source, review,
    effective date, or target resolution before they can influence legal
    retrieval as verified evidence.
    """
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    instruments = db.query(LegalInstrument).filter_by(knowledge_base_id=kb_id).order_by(LegalInstrument.official_title).limit(500).all()
    relations = db.query(LegalInstrumentRelation).filter_by(knowledge_base_id=kb_id).limit(1000).all()
    instrument_by_id = {row.id: row for row in instruments}
    instrument_items = []
    for row in instruments:
        reasons = []
        if row.review_status != "verified": reasons.append("instrument_not_verified")
        if not row.source_uri and not row.source_reference: reasons.append("missing_source_provenance")
        if not row.effective_from: reasons.append("missing_effective_date")
        if reasons:
            instrument_items.append({"instrument_id": row.id, "document_id": row.document_id,
                                     "title": row.official_title, "status": row.status,
                                     "review_status": row.review_status, "reasons": reasons})
    relation_items = []
    for row in relations:
        reasons = []
        if row.target_instrument_id is None: reasons.append("target_unresolved")
        if row.review_status != "verified": reasons.append("relation_not_verified")
        if not row.evidence_quote: reasons.append("missing_evidence")
        if reasons:
            source = instrument_by_id.get(row.source_instrument_id)
            target = instrument_by_id.get(row.target_instrument_id) if row.target_instrument_id else None
            relation_items.append({"relation_id": row.id, "relation": row.relation,
                                   "source_instrument_id": row.source_instrument_id,
                                   "source_title": source.official_title if source else None,
                                   "target_instrument_id": row.target_instrument_id,
                                   "target_title": target.official_title if target else row.target_text,
                                   "target_provision": row.target_provision,
                                   "review_status": row.review_status, "origin": row.origin,
                                   "evidence_quote": row.evidence_quote, "confidence": row.confidence,
                                   "reasons": reasons})
    return {"knowledge_base_id": kb_id, "instrument_count": len(instruments),
            "relation_count": len(relations), "instrument_items": instrument_items,
            "relation_items": relation_items,
            "ready": not instrument_items and not relation_items}


@app.post("/api/v1/knowledge-bases/{kb_id}/legal-registry/resolve")
def resolve_legal_registry(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    result = resolve_instrument_statuses(db, kb_id)
    record_audit(db, "legal_registry.resolve", user.id, "knowledge_base", kb_id, result)
    db.commit()
    return result


@app.post("/api/v1/knowledge-bases/{kb_id}/graph/sync")
def sync_knowledge_base_graph(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    assert_kb_access(db, user, kb_id)
    totals = {"entities": 0, "relationships": 0}
    documents = db.query(Document).filter_by(knowledge_base_id=kb_id, status="completed").filter(Document.deleted_at.is_(None)).all()
    for document in documents:
        result = sync_lightrag_document_graph(db, document)
        totals["entities"] += result["entities"]
        totals["relationships"] += result["relationships"]
        result = sync_legal_document_graph(db, document)
        totals["entities"] += result["entities"]
        totals["relationships"] += result["relationships"]
    record_audit(db, "graph.sync", user.id, "knowledge_base", kb_id, {**totals, "documents": len(documents)})
    db.commit()
    return {"status": "success", "documents": len(documents), **totals}


@app.patch("/api/v1/relationships/{relationship_id}", response_model=RelationshipOut)
def update_relationship(relationship_id: str, payload: RelationshipUpdate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    relationship = db.get(Relationship, relationship_id)
    if relationship: assert_kb_access(db, user, relationship.knowledge_base_id)
    if not relationship or relationship.deleted_at:
        raise HTTPException(404, "Relationship not found")
    values = payload.model_dump(exclude_unset=True)
    for key, value in values.items(): setattr(relationship, key, value)
    db.add(GraphProjectionEvent(event_type="relationship", relationship_id=relationship.id)); record_audit(db, "relationship.update", user.id, "relationship", relationship.id, values); db.commit(); db.refresh(relationship)
    return relationship


@app.delete("/api/v1/relationships/{relationship_id}")
def delete_relationship(relationship_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    relationship = db.get(Relationship, relationship_id)
    if relationship: assert_kb_access(db, user, relationship.knowledge_base_id)
    if not relationship or relationship.deleted_at:
        raise HTTPException(404, "Relationship not found")
    relationship.deleted_at = datetime.utcnow(); record_audit(db, "relationship.delete", user.id, "relationship", relationship.id); db.commit()
    return {"status": "deleted", "relationship_id": relationship.id}


@app.get("/api/v1/knowledge-bases/{kb_id}/graph-layout")
def graph_layout(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    assert_kb_access(db, user, kb_id)
    rows = db.query(GraphNodeLayout).filter_by(knowledge_base_id=kb_id).all()
    return {"items": [{"entity_id": row.entity_id, "x": row.position_x, "y": row.position_y} for row in rows]}


@app.put("/api/v1/knowledge-bases/{kb_id}/graph-layout")
def save_graph_layout(kb_id: str, payload: GraphLayoutUpdate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    assert_kb_access(db, user, kb_id)
    valid = {row[0] for row in db.query(Entity.id).filter_by(knowledge_base_id=kb_id).filter(Entity.deleted_at.is_(None)).all()}
    if any(item.entity_id not in valid for item in payload.items):
        raise HTTPException(400, "Layout contains an entity outside this knowledge base")
    for item in payload.items:
        row = db.query(GraphNodeLayout).filter_by(knowledge_base_id=kb_id, entity_id=item.entity_id).first()
        if row: row.position_x, row.position_y = item.x, item.y
        else: db.add(GraphNodeLayout(knowledge_base_id=kb_id, entity_id=item.entity_id, position_x=item.x, position_y=item.y))
    record_audit(db, "graph.layout.save", user.id, "knowledge_base", kb_id, {"count": len(payload.items)}); db.commit()
    return {"status": "success", "count": len(payload.items)}


@app.get("/api/v1/entities/{entity_id}/graph")
def get_entity_graph(entity_id: str, depth: int = 1, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404, "Entity not found")
    assert_kb_access(db, user, entity.knowledge_base_id)
    return entity_graph(db, entity, max(1, min(depth, 3)))


@app.get("/api/v1/entities/{entity_id}/sources")
def get_entity_sources(entity_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404, "Entity not found")
    assert_kb_access(db, user, entity.knowledge_base_id)
    rows = db.query(EntitySource, Document).join(Document, Document.id == EntitySource.document_id).filter(EntitySource.entity_id == entity_id).all()
    return {"entity_id": entity_id, "sources": [{"document_id": document.id, "title": document.title, "excerpt": source.excerpt} for source, document in rows]}


@app.get("/api/v1/entities/{entity_id}/inspector")
def get_entity_inspector(entity_id: str, depth: int = 1, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404, "Entity not found")
    assert_kb_access(db, user, entity.knowledge_base_id)
    """Return the evidence-backed context used by the graph inspector.

    The graph payload intentionally stays lightweight.  This endpoint is lazy
    loaded after a node click and is the canonical place to expose legal
    provenance, document/version context and bounded neighbours.
    """
    entity = db.get(Entity, entity_id)
    if not entity or entity.deleted_at:
        raise HTTPException(404, "Entity not found")
    depth = max(1, min(depth, 3))
    source_rows = db.query(EntitySource, Document).join(Document, Document.id == EntitySource.document_id).filter(
        EntitySource.entity_id == entity.id, Document.deleted_at.is_(None)
    ).order_by(EntitySource.created_at.desc()).limit(50).all()
    document_ids = {document.id for _, document in source_rows}
    attr_document_id = (entity.attributes or {}).get("document_id")
    if attr_document_id:
        document_ids.add(attr_document_id)
    documents = db.query(Document).filter(Document.id.in_(document_ids)).all() if document_ids else []
    documents_by_id = {document.id: document for document in documents}
    instruments = db.query(LegalInstrument).filter(LegalInstrument.document_id.in_(document_ids)).all() if document_ids else []
    instrument_by_document = {instrument.document_id: instrument for instrument in instruments}
    context_documents = []
    for document_id in sorted(document_ids):
        document = documents_by_id.get(document_id)
        if not document:
            continue
        instrument = instrument_by_document.get(document_id)
        context_documents.append({
            "document_id": document.id,
            "title": document.title or document.original_filename,
            "filename": document.original_filename,
            "document_type": document.document_type,
            "status": document.status,
            "instrument": LegalInstrumentOut.model_validate(instrument).model_dump() if instrument else None,
        })
    edges = db.query(Relationship).filter(
        Relationship.knowledge_base_id == entity.knowledge_base_id,
        Relationship.deleted_at.is_(None),
        (Relationship.source_entity_id == entity.id) | (Relationship.target_entity_id == entity.id),
    ).order_by(Relationship.relationship_type).limit(200).all()
    related_ids = {edge.source_entity_id for edge in edges} | {edge.target_entity_id for edge in edges}
    related_entities = db.query(Entity).filter(Entity.id.in_(related_ids), Entity.deleted_at.is_(None)).all() if related_ids else []
    related_by_id = {row.id: row for row in related_entities}
    edge_source_rows = db.query(RelationshipSource, Document).join(Document, Document.id == RelationshipSource.document_id).filter(
        RelationshipSource.relationship_id.in_([edge.id for edge in edges]) if edges else False
    ).all()
    edge_sources: dict[str, list[dict]] = {}
    for source, document in edge_source_rows:
        edge_sources.setdefault(source.relationship_id, []).append({
            "document_id": document.id, "title": document.title or document.original_filename, "excerpt": source.excerpt,
        })
    def edge_view(edge: Relationship) -> dict:
        other_id = edge.target_entity_id if edge.source_entity_id == entity.id else edge.source_entity_id
        other = related_by_id.get(other_id)
        return {
            **RelationshipOut.model_validate(edge).model_dump(),
            "direction": "outgoing" if edge.source_entity_id == entity.id else "incoming",
            "other_entity": {"id": other.id, "name": other.name, "entity_type": other.entity_type, "review_status": other.review_status} if other else None,
            "sources": edge_sources.get(edge.id, []),
        }
    family = []
    relation_rows = []
    for instrument in instruments:
        if instrument.family_id:
            members = db.query(LegalInstrument).filter_by(family_id=instrument.family_id, knowledge_base_id=entity.knowledge_base_id).order_by(LegalInstrument.effective_from, LegalInstrument.version_date).all()
            family.extend(LegalInstrumentOut.model_validate(row).model_dump() for row in members if row.id not in {item.get("id") for item in family})
        relation_rows.extend(db.query(LegalInstrumentRelation).filter(
            (LegalInstrumentRelation.source_instrument_id == instrument.id) | (LegalInstrumentRelation.target_instrument_id == instrument.id)
        ).all())
    warning_list = ["Entity has no stored evidence."] if not source_rows else []
    for instrument in instruments:
        if instrument.status in {"superseded", "repealed", "amended"}:
            warning_list.append(f"Instrument status is {instrument.status}; verify the applicable version before relying on it.")
    if any(edge.review_status == "suggested" for edge in edges):
        warning_list.append("Some connected relationships are suggestions and require review.")
    return {
        "entity": EntityOut.model_validate(entity).model_dump(),
        "context": {"documents": context_documents, "instruments": [LegalInstrumentOut.model_validate(row).model_dump() for row in instruments]},
        "evidence": [{"document_id": document.id, "title": document.title or document.original_filename, "excerpt": source.excerpt} for source, document in source_rows],
        "relationships": {"incoming": [edge_view(edge) for edge in edges if edge.target_entity_id == entity.id], "outgoing": [edge_view(edge) for edge in edges if edge.source_entity_id == entity.id]},
        "versions": {"family": family, "relations": [{"id": row.id, "relation": row.relation, "target_instrument_id": row.target_instrument_id, "target_provision": row.target_provision, "review_status": row.review_status, "confidence": row.confidence, "evidence_quote": row.evidence_quote} for row in relation_rows]},
        "analysis": {"depth": depth, "neighbour_count": len(related_entities), "warnings": warning_list},
    }


@app.post("/api/v1/query/impact")
def query_impact(payload: ImpactRequest, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    visible = kb_ids_visible_to(db, user)
    if payload.knowledge_base_ids and not set(payload.knowledge_base_ids).issubset(visible):
        raise HTTPException(404, "Knowledge base not found")
    return analyze_impact(db, payload.subject, payload.knowledge_base_ids, payload.max_depth, payload.include_indirect, payload.entity_id)


@app.post("/api/v1/tokens", response_model=TokenCreated)
def create_token(payload: TokenCreate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    # An unknown name would silently grant nothing now that authorize() requires
    # positive membership, so a typo is rejected at issue time instead.
    if set(payload.allowed_tools) - {tool["name"] for tool in MCP_TOOLS}:
        raise HTTPException(400, {"code": "TOKEN_TOOL_UNKNOWN", "message": "Token includes a tool that does not exist.", "retryable": False})
    if set(payload.allowed_scopes) - {INGEST_SCOPE}:
        raise HTTPException(400, {"code": "TOKEN_SCOPE_UNKNOWN", "message": "Token includes a scope that does not exist.", "retryable": False})
    # MCP tools and Ingest write access are managed on separate menus and must
    # never share a credential: a token that could both read via MCP and write
    # via Ingest would defeat the point of splitting them for easier management.
    # Checked before the Knowledge Base validity checks below so a conflicting
    # request is rejected for the real reason instead of a misleading
    # KNOWLEDGE_BASE_INACTIVE/allowed_knowledge_base_ids complaint.
    if payload.allowed_tools and INGEST_SCOPE in payload.allowed_scopes:
        raise HTTPException(400, {"code": "TOKEN_CAPABILITY_CONFLICT", "message": "A token may be scoped to MCP tools or to Ingest write access, not both.", "retryable": False})
    # allowed_knowledge_base_ids is the MCP read scope; a token with Ingest
    # write access carries no allowed_tools (enforced above), so that list
    # would never be consulted by authorize() and would only mislead an admin
    # reviewing the token's scope later. Drop it before the active-Knowledge-Base
    # check below so an ingest-only token is never rejected with a misleading
    # "MCP tokens can only be scoped to active Knowledge Bases" error because of
    # a read-scope list that is about to be discarded anyway.
    if INGEST_SCOPE in payload.allowed_scopes:
        payload.allowed_knowledge_base_ids = []
    # A token may only be granted a scope that is usable at the time it is issued.
    # Runtime authorization repeats this check so disabling a Knowledge Base also
    # takes effect for tokens that were created earlier.
    requested_kb_ids = set(payload.allowed_knowledge_base_ids)
    if requested_kb_ids:
        active_kb_ids = {
            row.id
            for row in db.query(KnowledgeBase.id).filter(
                KnowledgeBase.id.in_(requested_kb_ids),
                KnowledgeBase.status == "active",
                KnowledgeBase.deleted_at.is_(None),
            ).all()
        }
        if active_kb_ids != requested_kb_ids:
            raise HTTPException(status_code=400, detail={
                "code": "KNOWLEDGE_BASE_INACTIVE",
                "message": "MCP tokens can only be scoped to active Knowledge Bases.",
                "retryable": False,
            })
    # The ingest Knowledge Base is a separate axis from allowed_knowledge_base_ids
    # (the MCP read scope) and is required exactly when documents:write is
    # requested, so a write-scoped token can never be issued without knowing
    # where it may write, and a read-only token can never carry a dangling one.
    if INGEST_SCOPE in payload.allowed_scopes:
        if not payload.allowed_ingest_knowledge_base_id:
            raise HTTPException(400, {"code": "INGEST_KNOWLEDGE_BASE_REQUIRED", "message": "Write access requires exactly one Knowledge Base to ingest into.", "retryable": False})
        ingest_kb = db.query(KnowledgeBase).filter(
            KnowledgeBase.id == payload.allowed_ingest_knowledge_base_id,
            KnowledgeBase.status == "active", KnowledgeBase.deleted_at.is_(None),
        ).first()
        if not ingest_kb:
            raise HTTPException(400, {"code": "KNOWLEDGE_BASE_INACTIVE", "message": "The ingest Knowledge Base must be an active Knowledge Base.", "retryable": False})
    elif payload.allowed_ingest_knowledge_base_id:
        raise HTTPException(400, {"code": "INGEST_KNOWLEDGE_BASE_NOT_ALLOWED", "message": "An ingest Knowledge Base requires Write access.", "retryable": False})
    # An empty allowed_tools list used to mean "every tool" (wildcard), so an
    # issuer who left it blank still got a working MCP token. authorize() now
    # requires positive membership, so the same omission would silently mint a
    # credential that can do nothing. Reject it at issue time instead, once the
    # more specific ingest-scope checks above have had a chance to explain a
    # dangling allowed_ingest_knowledge_base_id first.
    if not payload.allowed_tools and INGEST_SCOPE not in payload.allowed_scopes:
        raise HTTPException(400, {"code": "TOKEN_NO_CAPABILITY", "message": "Token must be granted at least one MCP tool or Ingest write access.", "retryable": False})
    # RBAC: a token may never grant more Knowledge Base reach than its issuer
    # can see.  Applies to both axes — the MCP read list and the single ingest KB.
    visible = kb_ids_visible_to(db, user)
    requested_kb = set(payload.allowed_knowledge_base_ids) | ({payload.allowed_ingest_knowledge_base_id} if payload.allowed_ingest_knowledge_base_id else set())
    if not requested_kb.issubset(visible):
        raise HTTPException(403, {"code": "TOKEN_SCOPE_EXCEEDS_ROLE", "message": "Token scope may not exceed the Knowledge Bases available to your account.", "retryable": False})
    secret = create_token_secret()
    token = TokenKey(name=payload.name, description=payload.description, token_prefix=secret[:16], token_hash=token_digest(secret),
                     allowed_knowledge_base_ids=payload.allowed_knowledge_base_ids, allowed_tools=payload.allowed_tools,
                     allowed_scopes=payload.allowed_scopes, allowed_ingest_knowledge_base_id=payload.allowed_ingest_knowledge_base_id,
                     expires_at=payload.expires_at, requests_per_minute=payload.requests_per_minute,
                     max_concurrent_requests=payload.max_concurrent_requests, query_timeout_seconds=payload.query_timeout_seconds,
                     created_by=user.id)
    db.add(token); db.flush(); record_audit(db, "token.create", user.id, "token", token.id, {"name": token.name}); db.commit(); db.refresh(token)
    return {**TokenOut.model_validate(token).model_dump(), "token": secret}


@app.get("/api/v1/tokens", response_model=list[TokenOut])
def list_tokens(user: User = Depends(current_admin), db: Session = Depends(get_db)):
    rows = db.query(TokenKey).order_by(TokenKey.created_at.desc()).all()
    return [row for row in rows if token_visible_to(db, user, row)]


@app.post("/api/v1/tokens/{token_id}/disable")
def disable_token(token_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    token = db.get(TokenKey, token_id)
    if not token or not token_visible_to(db, user, token):
        raise HTTPException(404, "Token not found")
    if token.status == "revoked":
        raise HTTPException(409, "A revoked token cannot be enabled or disabled")
    token.status = "inactive"
    record_audit(db, "token.disable", user.id, "token", token.id); db.commit()
    return {"status": "success"}


@app.post("/api/v1/tokens/{token_id}/enable")
def enable_token(token_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    token = db.get(TokenKey, token_id)
    if not token or not token_visible_to(db, user, token):
        raise HTTPException(404, "Token not found")
    if token.status == "revoked":
        raise HTTPException(409, "A revoked token cannot be enabled")
    token.status = "active"
    record_audit(db, "token.enable", user.id, "token", token.id); db.commit()
    return {"status": "success"}


@app.post("/api/v1/tokens/{token_id}/revoke")
def revoke_token(token_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    token = db.get(TokenKey, token_id)
    if not token or not token_visible_to(db, user, token):
        raise HTTPException(404, "Token not found")
    token.status = "revoked"
    token.revoked_at = datetime.utcnow()
    record_audit(db, "token.revoke", user.id, "token", token.id); db.commit()
    return {"status": "success"}


@app.post("/api/v1/tokens/{token_id}/rotate", response_model=TokenCreated)
def rotate_token(token_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    """Issue a replacement secret without ever recovering the old plaintext key.

    Token hashes are intentionally one-way. Rotation preserves the original
    scope and limits, revokes the old credential atomically, and returns the new
    secret exactly once in the response.
    """
    previous = db.get(TokenKey, token_id)
    if not previous or not token_visible_to(db, user, previous):
        raise HTTPException(404, "Token not found")
    if previous.status == "revoked":
        raise HTTPException(409, {"code": "TOKEN_ALREADY_REVOKED", "message": "A revoked token cannot be rotated.", "retryable": False})
    # Defense-in-depth: create_token() already enforces that MCP tools and Ingest
    # write access never share a credential, but there is no DB-level constraint
    # backing that invariant. Re-check here so rotating a row that somehow ended
    # up mixed (e.g. a manual DB edit) doesn't silently mint a replacement with
    # the same conflict instead of surfacing it.
    if previous.allowed_tools and INGEST_SCOPE in (previous.allowed_scopes or []):
        raise HTTPException(400, {"code": "TOKEN_CAPABILITY_CONFLICT", "message": "A token may be scoped to MCP tools or to Ingest write access, not both.", "retryable": False})
    requested_kb_ids = set(previous.allowed_knowledge_base_ids or [])
    if requested_kb_ids:
        active_kb_ids = {
            row.id for row in db.query(KnowledgeBase.id).filter(
                KnowledgeBase.id.in_(requested_kb_ids), KnowledgeBase.status == "active", KnowledgeBase.deleted_at.is_(None),
            ).all()
        }
        if active_kb_ids != requested_kb_ids:
            raise HTTPException(status_code=400, detail={
                "code": "KNOWLEDGE_BASE_INACTIVE",
                "message": "The token includes a Knowledge Base that is no longer active; activate it or create a new scope before rotating.",
                "retryable": False,
            })
    if previous.allowed_ingest_knowledge_base_id:
        ingest_kb = db.query(KnowledgeBase).filter(
            KnowledgeBase.id == previous.allowed_ingest_knowledge_base_id,
            KnowledgeBase.status == "active", KnowledgeBase.deleted_at.is_(None),
        ).first()
        if not ingest_kb:
            raise HTTPException(status_code=400, detail={
                "code": "KNOWLEDGE_BASE_INACTIVE",
                "message": "The token's ingest Knowledge Base is no longer active; activate it or create a new scope before rotating.",
                "retryable": False,
            })
    secret = create_token_secret()
    replacement = TokenKey(
        name=previous.name, description=previous.description, token_prefix=secret[:16], token_hash=token_digest(secret),
        allowed_knowledge_base_ids=list(previous.allowed_knowledge_base_ids or []), allowed_tools=list(previous.allowed_tools or []),
        allowed_scopes=list(previous.allowed_scopes or []), allowed_ingest_knowledge_base_id=previous.allowed_ingest_knowledge_base_id,
        expires_at=previous.expires_at, requests_per_minute=previous.requests_per_minute,
        max_concurrent_requests=previous.max_concurrent_requests, query_timeout_seconds=previous.query_timeout_seconds,
        # Preserve a disabled credential's safety posture. Rotating a key must
        # not be a way to bypass an administrator's Disable action.
        status=previous.status,
        created_by=previous.created_by or user.id,
    )
    previous.status, previous.revoked_at = "revoked", datetime.utcnow()
    db.add(replacement); db.flush()
    record_audit(db, "token.rotate", user.id, "token", replacement.id, {
        "replaced_token_id": previous.id, "name": previous.name, "allowed_knowledge_base_count": len(requested_kb_ids),
    })
    db.commit(); db.refresh(replacement)
    return {**TokenOut.model_validate(replacement).model_dump(), "token": secret}


def authorized_query(payload: QueryRequest, token: TokenKey | None, db: Session):
    kb_ids = payload.knowledge_base_ids or (token.allowed_knowledge_base_ids if token else [])
    if token: authorize(token, "search_knowledge", kb_ids)
    return build_query_result(db, payload.query, kb_ids, payload.max_sources, token.id if token else None, payload.filters)


def effective_mcp_knowledge_base_ids(db: Session, token: TokenKey) -> list[str]:
    """Derive MCP retrieval scope from the token, never from client arguments.

    A scoped token searches its currently active Knowledge Bases. Legacy
    unscoped tokens retain an explicit all-active behaviour for compatibility.
    """
    configured_ids = list(token.allowed_knowledge_base_ids or [])
    rows = db.query(KnowledgeBase.id).filter(
        KnowledgeBase.deleted_at.is_(None), KnowledgeBase.status == "active",
    )
    if configured_ids:
        active_scope = [row[0] for row in rows.filter(KnowledgeBase.id.in_(configured_ids)).all()]
        if not active_scope:
            raise HTTPException(403, {
                "code": "KNOWLEDGE_BASE_INACTIVE",
                "message": "None of this MCP key's Knowledge Bases are active.",
                "retryable": False,
            })
        return active_scope
    return [row[0] for row in rows.all()]


def active_mcp_knowledge_base_ids(db: Session, kb_ids: list[str]) -> list[str]:
    """Validate historical result sources before MCP returns them."""
    rows = db.query(KnowledgeBase.id).filter(
        KnowledgeBase.deleted_at.is_(None), KnowledgeBase.status == "active",
        KnowledgeBase.id.in_(kb_ids),
    ).all()
    active_ids = {row[0] for row in rows}
    if set(kb_ids) - active_ids:
        raise HTTPException(403, {
            "code": "KNOWLEDGE_BASE_INACTIVE",
            "message": "The requested Knowledge Base is disabled, draft, deleted, or unavailable to MCP.",
            "retryable": False,
        })
    return list(kb_ids)


@app.post("/api/v1/query")
def admin_query(payload: QueryRequest, request: Request, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    visible = kb_ids_visible_to(db, user)
    if payload.knowledge_base_ids:
        if not set(payload.knowledge_base_ids).issubset(visible):
            raise HTTPException(404, "Knowledge base not found")
    else:
        # Unscoped searches stay within the caller's visible Knowledge Bases.
        payload = payload.model_copy(update={"knowledge_base_ids": sorted(visible)})
        if not payload.knowledge_base_ids:
            raise HTTPException(404, {"code": "KNOWLEDGE_BASE_NOT_VISIBLE", "message": "No Knowledge Base is available to this account yet.", "retryable": False})
    result = authorized_query(payload, None, db)
    record_retrieval_execution(db, request.state.request_id, result, actor_id=user.id)
    db.commit()
    return result


@app.get("/api/v1/query/results/{result_id}/sources")
def get_sources(result_id: str, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    saved = db.get(QueryResult, result_id)
    if not saved or saved.expires_at < datetime.utcnow(): raise HTTPException(404, "Result not found")
    return {"result_id": result_id, "sources": saved.result_json.get("sources", [])}


@app.post("/api/v1/query/results/{result_id}/feedback")
def query_feedback(result_id: str, payload: QueryFeedbackCreate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    saved = db.get(QueryResult, result_id)
    if not saved or saved.expires_at < datetime.utcnow():
        raise HTTPException(404, "Result not found")
    feedback = QueryFeedback(result_id=result_id, user_id=user.id, rating=payload.rating, comment=payload.comment)
    db.add(feedback); record_audit(db, "query.feedback", user.id, "query_result", result_id, {"rating": payload.rating}); db.commit()
    return {"status": "success", "feedback_id": feedback.id}


@app.post("/api/v1/system/test-openrouter")
def test_openrouter(_: User = Depends(current_admin)):
    try:
        return OpenRouterClient().check()
    except RuntimeError as exc:
        raise HTTPException(503, {"code": str(exc), "message": "OpenRouter configuration check failed", "retryable": True})


@app.get("/api/v1/system/graph-projection")
def graph_projection_status(_: User = Depends(current_admin), db: Session = Depends(get_db)):
    rows = db.query(GraphProjectionEvent.status, __import__("sqlalchemy").func.count()).group_by(GraphProjectionEvent.status).all()
    return {"status": "success", "events": {status: count for status, count in rows}}


@app.post("/api/v1/system/observability/cleanup")
def observability_cleanup(user: User = Depends(current_admin), db: Session = Depends(get_db)):
    """Run the bounded observability retention job on demand."""
    result = prune_observability(db)
    record_audit(db, "observability.cleanup", user.id, "system", "observability", result)
    db.commit()
    return {"status": "success", **result}


@app.get("/api/v1/audit-logs")
def list_audit_logs(limit: int = 100, user: User = Depends(require_manager), db: Session = Depends(get_db)):
    rows = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(max(limit, 1), 500)).all()
    # Managers see group-relevant rows only (target KB in their group, or their
    # own actions); admins see everything.
    if user.role != ROLE_ADMIN:
        group_kbs = kb_ids_visible_to(db, user)
        rows = [row for row in rows if (row.target_type == "knowledge_base" and row.target_id in group_kbs) or row.actor_user_id == user.id]
    return [{"id": row.id, "action": row.action, "actor_user_id": row.actor_user_id, "target_type": row.target_type,
             "target_id": row.target_id, "metadata": row.metadata_json, "created_at": row.created_at} for row in rows]


@app.get("/api/v1/logs/transactions")
def list_request_transactions(limit: int = 100, cursor: str | None = None, from_ts: str | None = None,
                              to_ts: str | None = None, method: str | None = None, status_code: int | None = None,
                              paginate: bool = False, user: User = Depends(require_manager), db: Session = Depends(get_db)):
    """Return recent request transactions for the operator UI.

    Records contain only request metadata collected by the middleware. Request
    bodies, authorization headers, cookies, prompts, and token values are never
    persisted or returned here.
    """
    bounded_limit = min(max(limit, 1), 200)
    query = db.query(AuditLog).filter(AuditLog.action == REQUEST_TRANSACTION_ACTION).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if (parsed := _parse_log_cursor(cursor)): query = query.filter(AuditLog.created_at < parsed)
    if (parsed := _parse_log_cursor(from_ts)): query = query.filter(AuditLog.created_at >= parsed)
    if (parsed := _parse_log_cursor(to_ts)): query = query.filter(AuditLog.created_at <= parsed)
    if method: query = query.filter(AuditLog.metadata_json["method"].as_string() == method)
    if status_code is not None: query = query.filter(AuditLog.metadata_json["status_code"].as_integer() == status_code)
    rows = query.limit(bounded_limit + 1).all()
    has_more = len(rows) > bounded_limit
    rows = rows[:bounded_limit]
    request_ids = [(row.metadata_json or {}).get("request_id") or row.target_id for row in rows]
    execution_rows = db.query(AuditLog).filter(
        AuditLog.action == "retrieval.execution", AuditLog.target_id.in_([item for item in request_ids if item]),
    ).order_by(AuditLog.created_at.desc()).all() if request_ids else []
    executions = {row.target_id: row.metadata_json for row in execution_rows}
    items = [{
        "id": row.id,
        "request_id": (row.metadata_json or {}).get("request_id") or row.target_id,
        "method": (row.metadata_json or {}).get("method", "UNKNOWN"),
        "path": (row.metadata_json or {}).get("path", ""),
        "status_code": (row.metadata_json or {}).get("status_code", 0),
        "duration_ms": (row.metadata_json or {}).get("duration_ms", 0),
        "authentication": (row.metadata_json or {}).get("authentication", "unknown"),
        "retrieval": executions.get((row.metadata_json or {}).get("request_id") or row.target_id),
        "created_at": row.created_at,
    } for row in rows]
    if paginate:
        return {"items": items, "next_cursor": rows[-1].created_at.isoformat() if has_more and rows else None,
                "has_more": has_more, "limit": bounded_limit}
    return items


def trace_spans(metadata: dict) -> list[dict]:
    """Normalize current spans and give historical audit entries a readable fallback."""
    cursor = 0
    spans = []
    for index, raw in enumerate(metadata.get("retrieval_trace") or [], 1):
        span = dict(raw)
        duration = int(span.get("duration_ms", 0) or 0)
        span.setdefault("span_id", f"span-{index}")
        span.setdefault("parent_span_id", "root")
        span.setdefault("offset_ms", cursor)
        cursor = max(cursor, int(span["offset_ms"] or 0) + duration)
        spans.append(span)
    return spans


def trace_summary(row: AuditLog) -> dict:
    metadata = row.metadata_json or {}
    spans = trace_spans(metadata)
    total_duration = max((int(span.get("offset_ms", 0)) + int(span.get("duration_ms", 0)) for span in spans), default=0)
    return {
        "trace_id": metadata.get("trace_id") or row.target_id,
        "request_id": metadata.get("request_id") or row.target_id,
        "transport": metadata.get("transport", "api"),
        "tool": metadata.get("tool"),
        "status": metadata.get("trace_status", "success"),
        "intent": (metadata.get("retrieval_plan") or {}).get("intent"),
        "knowledge_base_ids": metadata.get("knowledge_base_ids") or [],
        "source_count": metadata.get("source_count", 0),
        "duration_ms": total_duration,
        "request_summary": metadata.get("request_summary") or {},
        "response_summary": metadata.get("response_summary") or {},
        "created_at": row.created_at,
    }


def trace_run_summary(row: TraceRun) -> dict:
    """Serialize the normalized hot trace index without loading span payloads."""
    plan = row.retrieval_plan or {}
    return {
        "trace_id": row.id,
        "request_id": row.request_id or row.id,
        "transport": row.transport,
        "tool": row.tool,
        "status": row.trace_status,
        "intent": plan.get("intent"),
        "knowledge_base_ids": row.knowledge_base_ids or [],
        "source_count": row.source_count,
        "duration_ms": row.duration_ms,
        "request_summary": row.request_summary or {},
        "response_summary": row.response_summary or {},
        "created_at": row.created_at,
    }


def _parse_log_cursor(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(400, {"code": "LOG_CURSOR_INVALID", "message": "cursor must be an ISO timestamp", "retryable": False})


@app.get("/api/v1/traces")
def list_retrieval_traces(limit: int = 100, transport: str | None = None, status: str | None = None,
                          tool: str | None = None, search: str | None = None, cursor: str | None = None,
                          from_ts: str | None = None, to_ts: str | None = None, paginate: bool = False,
                          user: User = Depends(require_manager), db: Session = Depends(get_db)):
    """List safe RetrievalExecutor trace summaries for the Trace Explorer."""
    bounded_limit = min(max(limit, 1), 200)
    query = db.query(TraceRun).order_by(TraceRun.created_at.desc(), TraceRun.id.desc())
    if transport: query = query.filter(TraceRun.transport == transport)
    if status: query = query.filter(TraceRun.trace_status == status)
    if tool: query = query.filter(TraceRun.tool == tool)
    if (parsed := _parse_log_cursor(cursor)): query = query.filter(TraceRun.created_at < parsed)
    if (parsed := _parse_log_cursor(from_ts)): query = query.filter(TraceRun.created_at >= parsed)
    if (parsed := _parse_log_cursor(to_ts)): query = query.filter(TraceRun.created_at <= parsed)
    needle = (search or "").strip()
    if needle:
        pattern = f"%{needle}%"
        query = query.filter(or_(TraceRun.id.ilike(pattern), TraceRun.request_id.ilike(pattern),
                                 TraceRun.transport.ilike(pattern), TraceRun.tool.ilike(pattern)))
    rows = query.limit(bounded_limit + 1).all()
    has_more = len(rows) > bounded_limit
    rows = rows[:bounded_limit]
    items = [trace_run_summary(row) for row in rows]
    next_cursor = rows[-1].created_at.isoformat() if has_more and rows else None
    if paginate:
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more, "limit": bounded_limit}
    return items


@app.get("/api/v1/traces/{trace_id}")
def get_retrieval_trace(trace_id: str, user: User = Depends(require_manager), db: Session = Depends(get_db)):
    """Return a root span and safe child spans for one retrieval execution."""
    run = db.get(TraceRun, trace_id)
    if run:
        spans = db.query(TraceSpan).filter(TraceSpan.trace_id == trace_id).order_by(TraceSpan.offset_ms, TraceSpan.created_at).all()
        summary = trace_run_summary(run)
        return {**summary, "root_span": {"span_id": "root", "name": run.tool or "knowledge query",
                                          "status": run.trace_status, "duration_ms": run.duration_ms},
                "retrieval_plan": run.retrieval_plan, "spans": [{"span_id": span.span_id, "parent_span_id": span.parent_span_id,
                    "channel": span.channel, "system": span.system, "status": span.status, "result_count": span.result_count,
                    "duration_ms": span.duration_ms, "offset_ms": span.offset_ms, "reason_code": span.reason_code,
                    "detail": span.detail, "input_summary": span.input_summary, "output_summary": span.output_summary} for span in spans]}
    row = db.query(AuditLog).filter(AuditLog.action == "retrieval.execution", AuditLog.target_id == trace_id).order_by(AuditLog.created_at.desc()).first()
    if not row: raise HTTPException(404, "Trace not found")
    metadata = row.metadata_json or {}; summary = trace_summary(row)
    return {**summary, "root_span": {"span_id": "root", "name": metadata.get("tool") or "knowledge query", "status": summary["status"], "duration_ms": summary["duration_ms"]},
            "retrieval_plan": metadata.get("retrieval_plan"), "spans": trace_spans(metadata)}


@app.get("/api/v1/mcp/activity")
def list_mcp_activity(limit: int = 50, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    """Return operator-safe MCP tool traces, newest first.

    Token secrets and request headers are never persisted. Query text is capped
    so this remains an audit trail rather than a second document store.
    """
    rows = db.query(AuditLog).filter(AuditLog.action.in_(["mcp.tool.call", "mcp.tool.error"])).order_by(
        AuditLog.created_at.desc()
    ).limit(min(max(limit, 1), 200)).all()
    return [{"id": row.id, "action": row.action, "target_id": row.target_id,
             "metadata": row.metadata_json, "created_at": row.created_at} for row in rows]


MCP_TOOLS = [
    {"name": "search_knowledge", "description": "Search knowledge bases with automatic retrieval planning", "inputSchema": QueryRequest.model_json_schema()},
    {"name": "document_inventory_summary", "description": "Count and group non-deleted documents from the scoped document and legal registries", "inputSchema": DocumentInventoryRequest.model_json_schema()},
    {"name": "find_entities", "description": "Find entities by name or alias", "inputSchema": {"type": "object", "properties": {"search_text": {"type": "string"}}, "required": ["search_text"]}},
    {"name": "analyze_relationships", "description": "Analyze entity relationships", "inputSchema": {"type": "object", "properties": {"subjects": {"type": "array"}, "question": {"type": "string"}}, "required": ["subjects", "question"]}},
    {"name": "analyze_impact", "description": "Analyze direct and indirect impact", "inputSchema": ImpactRequest.model_json_schema()},
    {"name": "get_sources", "description": "Retrieve sources for a result", "inputSchema": {"type": "object", "properties": {"result_id": {"type": "string"}}, "required": ["result_id"]}},
    {"name": "resolve_legal_context", "description": "Resolve the in-force legal instruments and provisions relevant to a query within this MCP key's scope", "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 10000}, "as_of_date": {"type": "string", "format": "date"}, "include_historical": {"type": "boolean"}}, "required": ["query"]}},
    {"name": "get_legal_instrument", "description": "Get one legal instrument, its family and reviewed cross-document relations", "inputSchema": {"type": "object", "properties": {"instrument_id": {"type": "string"}}, "required": ["instrument_id"]}},
    {"name": "get_provision_history", "description": "List document versions containing a legal provision, scoped to the MCP key", "inputSchema": {"type": "object", "properties": {"instrument_id": {"type": "string"}, "provision_number": {"type": "string", "maxLength": 120}}, "required": ["instrument_id", "provision_number"]}},
]


def _mcp_scoped_instrument(db: Session, token: TokenKey, instrument_id: str, effective_kb_ids: list[str]) -> LegalInstrument:
    instrument = db.get(LegalInstrument, instrument_id)
    if not instrument or instrument.knowledge_base_id not in set(effective_kb_ids):
        raise HTTPException(404, {"code": "LEGAL_INSTRUMENT_NOT_FOUND", "message": "Legal instrument is not available in this MCP scope.", "retryable": False})
    return instrument


def _legal_relation_payload(db: Session, relation: LegalInstrumentRelation) -> dict[str, Any]:
    return {"id": relation.id, "relation": relation.relation, "source_instrument_id": relation.source_instrument_id,
            "target_instrument_id": relation.target_instrument_id, "target_text": relation.target_text,
            "target_provision": relation.target_provision, "evidence_quote": relation.evidence_quote,
            "confidence": relation.confidence, "origin": relation.origin, "review_status": relation.review_status}


def mcp_error(request_id: Any, code: str, message: str, *, retryable: bool = False):
    return JSONResponse(status_code=200, content={"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message, "retryable": retryable}})


def mcp_audit_arguments(name: str, arguments: dict[str, Any], effective_kb_ids: list[str] | None = None) -> dict:
    """Keep request context useful for operators without storing credentials."""
    query = arguments.get("query") or arguments.get("search_text") or arguments.get("subject") or arguments.get("question")
    requested_kb_ids = arguments.get("knowledge_base_ids", [])
    item: dict[str, Any] = {"knowledge_base_ids": effective_kb_ids if effective_kb_ids is not None else requested_kb_ids}
    if effective_kb_ids is not None and requested_kb_ids:
        item["client_knowledge_base_ids_ignored"] = True
    if query is not None:
        value = str(query)
        item["query"] = value[:2000]
        item["query_truncated"] = len(value) > 2000
    if name == "analyze_relationships":
        item["subjects"] = [str(subject)[:500] for subject in arguments.get("subjects", [])[:20]]
    if name in {"get_legal_instrument", "get_provision_history"}:
        item["instrument_id"] = str(arguments.get("instrument_id", ""))[:100]
    if name == "get_provision_history":
        item["provision_number"] = str(arguments.get("provision_number", ""))[:120]
    return item


def record_mcp_tool_audit(db: Session, token: TokenKey, request_id: Any, name: str, arguments: dict[str, Any],
                          route: list[dict], started_at: float, *, retrieval_plan: dict | None = None,
                          error_code: str | None = None, effective_kb_ids: list[str] | None = None) -> None:
    metadata = {
        "request_id": str(request_id)[:100] if request_id is not None else None,
        "tool": name,
        "token_name": token.name,
        "duration_ms": round((time.monotonic() - started_at) * 1000),
        "route": route,
        **mcp_audit_arguments(name, arguments, effective_kb_ids),
    }
    if retrieval_plan:
        metadata["retrieval_plan"] = retrieval_plan
    if error_code:
        metadata["error_code"] = error_code
    record_audit(db, "mcp.tool.error" if error_code else "mcp.tool.call", None, "token", token.id, metadata)
    db.commit()


@app.post("/mcp")
async def mcp(request: Request, db: Session = Depends(get_db)):
    request_id = None
    token = None
    deadline_token = None
    acquired = False
    mcp_started_at = time.monotonic()
    tool_name = None
    tool_arguments: dict[str, Any] = {}
    try:
        body: dict[str, Any] = await request.json(); request_id = body.get("id")
        token = bearer_token(request, db); mcp_limiter.acquire(token); acquired = True
        deadline_token = set_deadline(token.query_timeout_seconds)
        method, params = body.get("method"), body.get("params", {})
        if method == "initialize": result = {"protocolVersion": "2025-03-26", "serverInfo": {"name": "softnix-knowledge", "version": "0.1.0"}, "capabilities": {"tools": {}}}
        elif method == "tools/list": result = {"tools": [tool for tool in MCP_TOOLS if tool["name"] in (token.allowed_tools or [])]}
        elif method == "tools/call":
            name = params.get("name"); arguments = params.get("arguments", {}); tool_name, tool_arguments = name, arguments
            authorize(token, name, list(token.allowed_knowledge_base_ids or []))
            effective_kb_ids = effective_mcp_knowledge_base_ids(db, token)
            if name == "search_knowledge":
                payload = QueryRequest.model_validate(arguments)
                payload.knowledge_base_ids = effective_kb_ids
                result = authorized_query(payload, token, db)
            elif name == "document_inventory_summary":
                payload = DocumentInventoryRequest.model_validate(arguments)
                result = build_document_inventory_result(
                    db, payload.query or "document inventory summary", effective_kb_ids, token_id=token.id,
                    scope=payload.scope, include_documents=payload.include_documents,
                    max_documents=payload.max_documents,
                )
            elif name == "find_entities":
                kb_ids = effective_kb_ids
                rows = db.query(Entity).filter(Entity.knowledge_base_id.in_(kb_ids), Entity.deleted_at.is_(None), Entity.name.ilike(f"%{arguments.get('search_text', '')}%")).limit(min(arguments.get("limit", 10), 50)).all()
                result = {"status": "success", "entities": [EntityOut.model_validate(row).model_dump() for row in rows]}
                result["metadata"] = {"retrieval_trace": [{"channel": "entity_lookup", "system": "PostgreSQL entity tables", "status": "used", "result_count": len(rows), "detail": "name and alias lookup"}]}
            elif name == "analyze_relationships":
                kb_ids = effective_kb_ids
                entity = resolve_entity(db, kb_ids, arguments.get("subjects", [""])[0])
                result = {"status": "success", "graph": entity_graph(db, entity, min(arguments.get("max_depth", 1), 3)) if entity else {"nodes": [], "edges": []}}
                result["metadata"] = {"retrieval_trace": [{"channel": "graph_relationships", "system": "PostgreSQL graph tables", "status": "used", "result_count": len(result["graph"]["edges"]), "detail": "bounded relationship traversal"}]}
            elif name == "analyze_impact":
                impact = ImpactRequest.model_validate(arguments)
                kb_ids = effective_kb_ids
                result = analyze_impact(db, impact.subject, kb_ids, impact.max_depth, impact.include_indirect)
                result["metadata"] = {"retrieval_trace": [{"channel": "graph_impact", "system": "PostgreSQL graph tables", "status": "used", "result_count": len(result.get("direct_impacts", [])) + len(result.get("indirect_impacts", [])), "detail": "bounded impact traversal"}]}
            elif name == "get_sources":
                saved = db.get(QueryResult, arguments.get("result_id"))
                if saved:
                    source_kb_ids = saved.result_json.get("metadata", {}).get("knowledge_base_ids", [])
                    authorize(token, "get_sources", source_kb_ids)
                    active_mcp_knowledge_base_ids(db, source_kb_ids)
                result = {"sources": saved.result_json.get("sources", [])} if saved else {"sources": []}
                result["metadata"] = {"retrieval_trace": [{"channel": "result_sources", "system": "PostgreSQL query result store", "status": "used", "result_count": len(result["sources"]), "detail": "stored cited sources"}]}
            elif name == "resolve_legal_context":
                query = str(arguments.get("query", "")).strip()
                if not query:
                    raise HTTPException(422, {"code": "LEGAL_QUERY_REQUIRED", "message": "query is required", "retryable": False})
                filters = {"as_of_date": arguments.get("as_of_date"), "include_historical": bool(arguments.get("include_historical", False))}
                payload = QueryRequest.model_validate({"query": query, "knowledge_base_ids": effective_kb_ids,
                                                       "filters": filters, "max_sources": 1})
                trace: list[dict] = []
                decision = build_retrieval_plan(db, query, effective_kb_ids, 1, payload.filters, trace)
                legal_context = decision.plan.legal_context
                result = {"status": "success", "text": "Legal context resolved from the MCP key's active Knowledge Base scope.",
                          "knowledge_base_ids": effective_kb_ids,
                          "legal_context": legal_context.model_dump(mode="json") if legal_context else None,
                          "retrieval_plan": decision.plan.model_dump(mode="json"),
                          "metadata": {"retrieval_plan": decision.plan.model_dump(mode="json"), "retrieval_trace": trace,
                                       "source_of_truth": "PostgreSQL legal registry"}}
            elif name == "get_legal_instrument":
                instrument = _mcp_scoped_instrument(db, token, str(arguments.get("instrument_id", "")), effective_kb_ids)
                family = db.query(LegalInstrument).filter_by(knowledge_base_id=instrument.knowledge_base_id, family_id=instrument.family_id).order_by(LegalInstrument.effective_from).all() if instrument.family_id else [instrument]
                outgoing = db.query(LegalInstrumentRelation).filter_by(source_instrument_id=instrument.id).all()
                incoming = db.query(LegalInstrumentRelation).filter_by(target_instrument_id=instrument.id).all()
                result = {"status": "success", "text": "Legal instrument and reviewed provenance loaded.",
                          "instrument": LegalInstrumentOut.model_validate(instrument).model_dump(),
                          "family": [LegalInstrumentOut.model_validate(row).model_dump() for row in family],
                          "outgoing_relations": [_legal_relation_payload(db, row) for row in outgoing],
                          "incoming_relations": [_legal_relation_payload(db, row) for row in incoming],
                          "knowledge_base_ids": [instrument.knowledge_base_id],
                          "metadata": {"retrieval_trace": [{"channel": "legal_registry", "system": "PostgreSQL legal registry", "status": "used", "result_count": 1 + len(outgoing) + len(incoming), "detail": "scope-checked instrument provenance"}]}}
            elif name == "get_provision_history":
                instrument = _mcp_scoped_instrument(db, token, str(arguments.get("instrument_id", "")), effective_kb_ids)
                number = str(arguments.get("provision_number", "")).strip()
                if not number:
                    raise HTTPException(422, {"code": "PROVISION_NUMBER_REQUIRED", "message": "provision_number is required", "retryable": False})
                family_rows = db.query(LegalInstrument).filter_by(knowledge_base_id=instrument.knowledge_base_id, family_id=instrument.family_id).order_by(LegalInstrument.effective_from).all() if instrument.family_id else [instrument]
                document_ids = [row.document_id for row in family_rows]
                entities = db.query(Entity).filter(Entity.knowledge_base_id == instrument.knowledge_base_id, Entity.entity_type == "Provision", Entity.deleted_at.is_(None)).all()
                family_doc_ids = set(document_ids)
                matched_entities = []
                for row in entities:
                    attrs = row.attributes or {}
                    if not any(f":provision:{doc_id}:" in row.identity_key for doc_id in family_doc_ids):
                        continue
                    if not provision_number_matches(number, str(attrs.get("provision_number") or "")):
                        continue
                    matched_entities.append(EntityOut.model_validate(row).model_dump())
                result = {"status": "success", "text": "Provision history is scoped to one legal instrument family.",
                          "instrument_id": instrument.id, "provision_number": number,
                          "versions": [LegalInstrumentOut.model_validate(row).model_dump() for row in family_rows],
                          "provisions": matched_entities, "knowledge_base_ids": [instrument.knowledge_base_id],
                          "metadata": {"retrieval_trace": [{"channel": "legal_registry", "system": "PostgreSQL legal registry", "status": "used", "result_count": len(family_rows) + len(matched_entities), "detail": "document-scoped provision identity"}]}}
            else: return mcp_error(request_id, "MCP_TOOL_NOT_FOUND", "Tool not found")
            route = result.get("metadata", {}).get("retrieval_trace", [])
            record_retrieval_execution(db, request.state.request_id, result, transport="mcp", tool=name,
                                       rpc_request_id=str(request_id) if request_id is not None else None)
            record_mcp_tool_audit(db, token, request_id, name, arguments, route, mcp_started_at,
                                  retrieval_plan=result.get("metadata", {}).get("retrieval_plan"),
                                  effective_kb_ids=effective_kb_ids)
            result.setdefault("request_id", request_id)
            result = {"content": [{"type": "text", "text": result.get("answer") or result.get("text", "Structured knowledge result available.")}], "structuredContent": result}
        else: return mcp_error(request_id, "MCP_METHOD_NOT_FOUND", "Method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "MCP_REQUEST_INVALID", "message": str(exc.detail)}
        error_code = detail.get("code", "MCP_REQUEST_INVALID")
        if token is not None and tool_name:
            record_mcp_tool_audit(db, token, request_id, tool_name, tool_arguments, [], mcp_started_at, error_code=error_code)
        record_mcp_error_trace(db, request.state.request_id, transport="mcp", tool=tool_name, error_code=error_code,
                               message=detail.get("message", "Request rejected"), duration_ms=round((time.monotonic() - mcp_started_at) * 1000),
                               query_preview=mcp_audit_arguments(tool_name, tool_arguments).get("query") if tool_name else None)
        db.commit()
        return mcp_error(request_id, error_code, detail.get("message", "Request rejected"), retryable=detail.get("retryable", False))
    except McpLimitExceeded as exc:
        if token is not None and tool_name:
            record_mcp_tool_audit(db, token, request_id, tool_name, tool_arguments, [], mcp_started_at, error_code=exc.code)
        record_mcp_error_trace(db, request.state.request_id, transport="mcp", tool=tool_name, error_code=exc.code,
                               message=exc.message, duration_ms=round((time.monotonic() - mcp_started_at) * 1000),
                               query_preview=mcp_audit_arguments(tool_name, tool_arguments).get("query") if tool_name else None)
        db.commit()
        return mcp_error(request_id, exc.code, exc.message, retryable=exc.code == "MCP_LIMIT_STORE_UNAVAILABLE")
    except RuntimeError as exc:
        if token is not None and tool_name:
            record_mcp_tool_audit(db, token, request_id, tool_name, tool_arguments, [], mcp_started_at, error_code=str(exc))
        record_mcp_error_trace(db, request.state.request_id, transport="mcp", tool=tool_name, error_code=str(exc),
                               message="Token query timeout exceeded" if str(exc) == "MCP_TIMEOUT" else "Tool execution failed",
                               duration_ms=round((time.monotonic() - mcp_started_at) * 1000),
                               query_preview=mcp_audit_arguments(tool_name, tool_arguments).get("query") if tool_name else None)
        db.commit()
        if str(exc) == "MCP_TIMEOUT": return mcp_error(request_id, "MCP_TIMEOUT", "Token query timeout exceeded", retryable=True)
        return mcp_error(request_id, "MCP_EXECUTION_FAILED", "Tool execution failed", retryable=True)
    except Exception:
        if token is not None and tool_name:
            record_mcp_tool_audit(db, token, request_id, tool_name, tool_arguments, [], mcp_started_at, error_code="MCP_REQUEST_INVALID")
        record_mcp_error_trace(db, request.state.request_id, transport="mcp", tool=tool_name, error_code="MCP_REQUEST_INVALID",
                               message="Invalid MCP request", duration_ms=round((time.monotonic() - mcp_started_at) * 1000),
                               query_preview=mcp_audit_arguments(tool_name, tool_arguments).get("query") if tool_name else None)
        db.commit()
        return mcp_error(request_id, "MCP_REQUEST_INVALID", "Invalid MCP request")
    finally:
        if deadline_token is not None: reset_deadline(deadline_token)
        if acquired and token is not None: mcp_limiter.release(token)


INGEST_MAX_BATCH_FILES = 20


def ingest_budget(token: TokenKey = Depends(ingest_token)):
    """Charge an ingest call against the token's shared rate and concurrency budget.

    The slot is released in a finally block because FastAPI runs post-yield
    cleanup even when the handler raises, so a rejected upload cannot leak a
    concurrency slot for the rest of the token's timeout window.
    """
    try:
        mcp_limiter.acquire(token)
    except McpLimitExceeded as exc:
        raise HTTPException(503 if exc.code == "MCP_LIMIT_STORE_UNAVAILABLE" else 429,
                            {"code": exc.code, "message": exc.message, "retryable": True})
    try:
        yield token
    finally:
        mcp_limiter.release(token)


def ingest_knowledge_base(db: Session, token: TokenKey, kb_id: str) -> KnowledgeBase:
    kb = db.get(KnowledgeBase, kb_id)
    # Out of scope answers 404 rather than 403 so a token cannot enumerate the
    # Knowledge Bases it was never granted. Ingest scope is a single Knowledge
    # Base, distinct from the MCP read list.
    if not kb or kb.deleted_at or kb_id != token.allowed_ingest_knowledge_base_id:
        raise HTTPException(404, {"code": "KNOWLEDGE_BASE_NOT_FOUND", "message": "Knowledge base not found", "retryable": False})
    if kb.status != "active":
        raise HTTPException(409, {"code": "KNOWLEDGE_BASE_DISABLED", "message": "Activate this Knowledge Base before uploading documents.", "retryable": False})
    return kb


def ingest_knowledge_base_view(kb: KnowledgeBase) -> dict:
    return {"id": kb.id, "code": kb.code, "name": kb.name, "status": kb.status}


def ingest_document_row(db: Session, token: TokenKey, document_id: str) -> Document:
    doc = db.get(Document, document_id)
    if not doc or doc.deleted_at or doc.knowledge_base_id != token.allowed_ingest_knowledge_base_id:
        raise HTTPException(404, {"code": "DOCUMENT_NOT_FOUND", "message": "Document not found", "retryable": False})
    return doc


def ingest_job_view(job: ProcessingJob) -> dict:
    return {"id": job.id, "type": job.job_type, "status": job.status, "stage": job.current_stage,
            "progress_percent": job.progress_percent, "attempt_count": job.attempt_count,
            "error_code": job.error_code, "error_message": job.error_message}


def ingest_document_view(db: Session, doc: Document) -> dict:
    job = db.query(ProcessingJob).filter_by(document_id=doc.id).order_by(ProcessingJob.created_at.desc()).first()
    return {"document_id": doc.id, "knowledge_base_id": doc.knowledge_base_id, "title": doc.title,
            "filename": doc.original_filename, "status": doc.status, "document_type": doc.document_type,
            "error_code": doc.error_code, "created_at": doc.created_at,
            "latest_job": ingest_job_view(job) if job else None}


def ingest_upload_error(exc: ValueError) -> HTTPException:
    code = str(exc)
    # A duplicate is a conflict rather than a bad request so a retrying client can
    # tell "already sent this" apart from "sent something invalid".
    status_code = 413 if code == "FILE_TOO_LARGE" else 409 if code == "FILE_DUPLICATE" else 400
    return HTTPException(status_code, {"code": code, "message": "Upload rejected", "retryable": False})


def record_ingest_audit(db: Session, token: TokenKey, doc: Document, kb_id: str, *, batch: bool = False) -> None:
    # Shares the UI action name so Logging groups both origins, with attribution
    # in metadata because a token call has no User row.
    record_audit(db, "document.upload", None, "document", doc.id, {
        "knowledge_base_id": kb_id, "filename": doc.original_filename, "document_type": doc.document_type,
        "transport": "ingest_api", "token_id": token.id, "token_name": token.name, "batch": batch,
    })


def record_ingest_rejection(db: Session, token: TokenKey, kb_id: str, filename: str | None, error_code: str) -> None:
    record_audit(db, "document.ingest.rejected", None, "token", token.id, {
        "knowledge_base_id": kb_id, "filename": filename, "error_code": error_code, "token_name": token.name,
    })
    db.commit()


@app.get("/api/v1/ingest/knowledge-bases")
def ingest_list_knowledge_bases(token: TokenKey = Depends(ingest_budget), db: Session = Depends(get_db)):
    """Report the (single) Knowledge Base this token may write to.

    An ingest token is scoped to exactly one Knowledge Base, so this always
    returns 0 or 1 items - never a directory of every tenant's KBs. A KB is
    still reported here even if disabled (status != "active") so a client can
    tell "nothing configured" apart from "configured but paused"; uploads to
    a disabled KB still fail with KNOWLEDGE_BASE_DISABLED.
    """
    kb = db.get(KnowledgeBase, token.allowed_ingest_knowledge_base_id)
    items = [ingest_knowledge_base_view(kb)] if kb and not kb.deleted_at else []
    return {"items": items}


@app.post("/api/v1/ingest/knowledge-bases/{kb_id}/documents", status_code=202)
def ingest_upload_document(kb_id: str, file: UploadFile = File(...), title: str | None = Form(None),
                           document_type: str = Form("general"), template_id: str | None = Form(None),
                           metadata_json: str | None = Form(None), published_at: date | None = Form(None),
                           token: TokenKey = Depends(ingest_budget), db: Session = Depends(get_db)):
    ingest_knowledge_base(db, token, kb_id)
    try:
        template, profile, metadata = _upload_metadata(template_id, document_type, metadata_json, db, kb_id)
        doc, job = create_document_job(db, kb_id, file, title, profile, published_at, template, metadata)
    except ValueError as exc:
        record_ingest_rejection(db, token, kb_id, file.filename, str(exc))
        raise ingest_upload_error(exc)
    # create_document_job() already committed the document; the audit row is
    # best-effort observability and must never turn an already-queued upload
    # into a lost document_id if this second commit fails.
    try:
        record_ingest_audit(db, token, doc, kb_id)
        db.commit()
    except Exception:
        db.rollback()
    return {"status": "queued", "document_id": doc.id, "job_id": job.id, "document_type": doc.document_type,
            "template_id": doc.metadata_template_id}


class IngestTextRequest(BaseModel):
    """Pre-extracted text payload for JSON-only senders (InsightDOC Custom API).

    The text is stored as a Markdown document and continues through the
    regular processing pipeline; no OCR runs because the layer is the text.
    """
    title: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1)
    document_type: str = "general"
    template_id: str | None = None
    metadata_json: str | None = None
    published_at: date | None = None


@app.post("/api/v1/ingest/knowledge-bases/{kb_id}/documents/text", status_code=202)
def ingest_upload_document_text(kb_id: str, body: IngestTextRequest,
                                token: TokenKey = Depends(ingest_budget), db: Session = Depends(get_db)):
    """Queue pre-extracted text (OCR/LLM output) as a Markdown document.

    Distinct error semantics from the file path: ``FILE_DUPLICATE`` remains
    a 409 so retrying senders can recognise "already sent", everything else
    about validation maps onto the same codes.
    """
    from .ingest_text import create_text_document_job
    ingest_knowledge_base(db, token, kb_id)
    try:
        template, profile, metadata = _upload_metadata(body.template_id, body.document_type, body.metadata_json, db, kb_id)
        doc, job = create_text_document_job(db, kb_id, body.title, body.text, profile, body.published_at,
                                            template, metadata)
    except ValueError as exc:
        record_ingest_rejection(db, token, kb_id, body.title, str(exc))
        raise ingest_upload_error(exc)
    try:
        record_ingest_audit(db, token, doc, kb_id)
        db.commit()
    except Exception:
        db.rollback()
    return {"status": "queued", "document_id": doc.id, "job_id": job.id, "document_type": doc.document_type,
            "template_id": doc.metadata_template_id}


@app.post("/api/v1/ingest/knowledge-bases/{kb_id}/documents/batch", status_code=202)
def ingest_upload_documents_batch(kb_id: str, files: list[UploadFile] = File(...), document_type: str = Form("general"),
                                  template_id: str | None = Form(None), metadata_json: str | None = Form(None),
                                  token: TokenKey = Depends(ingest_budget), db: Session = Depends(get_db)):
    """Queue a bounded batch where a per-file failure never discards the rest."""
    ingest_knowledge_base(db, token, kb_id)
    if not files:
        raise HTTPException(400, {"code": "BATCH_FILES_REQUIRED", "message": "Send at least one file.", "retryable": False})
    if len(files) > INGEST_MAX_BATCH_FILES:
        raise HTTPException(400, {"code": "BATCH_TOO_MANY_FILES", "message": f"A batch can contain at most {INGEST_MAX_BATCH_FILES} files.", "retryable": False})
    try:
        template, profile, metadata = _upload_metadata(template_id, document_type, metadata_json, db, kb_id)
    except ValueError as exc:
        record_ingest_rejection(db, token, kb_id, None, str(exc))
        raise HTTPException(400, {"code": str(exc), "message": "Document metadata is invalid.", "retryable": False})

    results = []
    for upload in files:
        filename = upload.filename or "unnamed-file"
        result = {"filename": filename, "status": "failed", "document_type": profile, "template_id": template.get("id")}
        try:
            doc, job = create_document_job(db, kb_id, upload, None, profile, None, template, metadata)
        except ValueError as exc:
            record_ingest_rejection(db, token, kb_id, filename, str(exc))
            result.update({"error_code": str(exc), "message": "Upload rejected"})
            results.append(result)
            continue
        except Exception:
            db.rollback()
            record_ingest_rejection(db, token, kb_id, filename, "UPLOAD_FAILED")
            result.update({"error_code": "UPLOAD_FAILED", "message": "Upload could not be queued"})
            results.append(result)
            continue
        # The document is already committed by create_document_job(); a failure
        # writing the (best-effort) audit row must not demote this file back to
        # failed and strand a queued document with no id in the response.
        result.update({"status": "queued", "document_id": doc.id, "job_id": job.id})
        try:
            record_ingest_audit(db, token, doc, kb_id, batch=True)
            db.commit()
        except Exception:
            db.rollback()
        results.append(result)

    queued_count = sum(item["status"] == "queued" for item in results)
    return {"status": "queued" if queued_count == len(results) else "partial", "document_type": profile,
            "template_id": template.get("id"), "total": len(results), "queued_count": queued_count,
            "failed_count": len(results) - queued_count, "results": results}


@app.get("/api/v1/ingest/knowledge-bases/{kb_id}/documents")
def ingest_list_documents(kb_id: str, status: str | None = None, limit: int = 50, offset: int = 0,
                          token: TokenKey = Depends(ingest_budget), db: Session = Depends(get_db)):
    ingest_knowledge_base(db, token, kb_id)
    if limit < 1 or limit > 100 or offset < 0:
        raise HTTPException(400, {"code": "DOCUMENT_PAGE_INVALID", "message": "limit must be 1-100 and offset must be non-negative.", "retryable": False})
    rows = db.query(Document).filter(Document.knowledge_base_id == kb_id, Document.deleted_at.is_(None))
    if status:
        rows = rows.filter(Document.status == status)
    total = rows.count()
    documents = rows.order_by(Document.created_at.desc()).offset(offset).limit(limit).all()
    return {"items": [ingest_document_view(db, doc) for doc in documents], "total": total, "limit": limit, "offset": offset}


@app.get("/api/v1/ingest/documents/{document_id}")
def ingest_document_status(document_id: str, token: TokenKey = Depends(ingest_budget), db: Session = Depends(get_db)):
    return ingest_document_view(db, ingest_document_row(db, token, document_id))


@app.get("/api/v1/ingest/documents/{document_id}/jobs")
def ingest_document_jobs(document_id: str, token: TokenKey = Depends(ingest_budget), db: Session = Depends(get_db)):
    doc = ingest_document_row(db, token, document_id)
    rows = db.query(ProcessingJob).filter_by(document_id=doc.id).order_by(ProcessingJob.created_at.desc()).all()
    return [ingest_job_view(job) for job in rows]
