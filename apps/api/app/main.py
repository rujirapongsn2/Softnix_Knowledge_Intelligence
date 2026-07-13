from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
import time

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
import httpx
import redis
from sqlalchemy.orm import Session

from .config import get_settings
from .audit import record_audit, record_retrieval_execution
from .db import Base, engine, get_db, SessionLocal
from .external_ocr import ExternalOcrClient
from .graph_store import Neo4jGraphStore
from .models import AuditLog, Document, Entity, EntitySource, GraphNodeLayout, GraphProjectionEvent, KnowledgeBase, ProcessingJob, QueryFeedback, QueryResult, Relationship, RelationshipSource, TokenKey, User
from .observability import metrics, now
from .openrouter import OpenRouterClient
from .mcp_limits import McpLimitExceeded, mcp_limiter
from .request_budget import reset_deadline, set_deadline
from .schemas import DocumentMetadataUpdate, DocumentOut, EntityCreate, EntityOut, EntityUpdate, GraphLayoutUpdate, ImpactRequest, KnowledgeBaseCreate, KnowledgeBaseOut, LegalMetadataUpdate, LegalRelationshipReview, LoginRequest, QueryFeedbackCreate, QueryRequest, RelationshipCreate, RelationshipOut, RelationshipUpdate, RetrievalConfigUpdate, TokenCreate, TokenCreated, TokenOut
from .security import authorize, bearer_token, create_session_token, create_token_secret, current_admin, password_hash, refresh_admin, token_digest, verify_password
from .services import DEFAULT_RETRIEVAL_CONFIG, analyze_impact, build_query_result, create_document_job, create_entity, create_relationship, entity_graph, process_next_job, queue_embedding_reindex, resolve_entity, sync_legal_document_graph, sync_lightrag_document_graph

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
    auth_type = "mcp_token" if request.headers.get("authorization", "").lower().startswith("bearer ") else (
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
            db.add(User(username=settings.initial_admin_username, password_hash=password_hash(settings.initial_admin_password)))
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
    if settings.ext_ocr_key:
        try:
            ExternalOcrClient(settings).check(); dependencies["external_ocr"] = "ready"
        except RuntimeError: failures["external_ocr"] = "unavailable"
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
def me(user: User = Depends(current_admin)): return {"id": user.id, "username": user.username}


@app.post("/api/v1/knowledge-bases", response_model=KnowledgeBaseOut)
def create_kb(payload: KnowledgeBaseCreate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if db.query(KnowledgeBase).filter_by(code=payload.code).first(): raise HTTPException(409, "Knowledge base code already exists")
    kb = KnowledgeBase(**payload.model_dump(), retrieval_config=DEFAULT_RETRIEVAL_CONFIG.copy())
    db.add(kb); db.flush(); record_audit(db, "knowledge_base.create", user.id, "knowledge_base", kb.id, {"code": kb.code}); db.commit(); db.refresh(kb); return kb


@app.get("/api/v1/knowledge-bases", response_model=list[KnowledgeBaseOut])
def list_kbs(_: User = Depends(current_admin), db: Session = Depends(get_db)):
    return db.query(KnowledgeBase).filter(KnowledgeBase.deleted_at.is_(None)).all()


@app.patch("/api/v1/knowledge-bases/{kb_id}/retrieval-config", response_model=KnowledgeBaseOut)
def update_retrieval_config(kb_id: str, payload: RetrievalConfigUpdate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    kb = db.get(KnowledgeBase, kb_id)
    if not kb or kb.deleted_at:
        raise HTTPException(404, "Knowledge base not found")
    try:
        kb.retrieval_config = payload.merged(kb.retrieval_config or {})
    except ValueError as exc:
        raise HTTPException(422, {"code": "RETRIEVAL_CONFIG_INVALID", "message": str(exc), "retryable": False}) from exc
    record_audit(db, "knowledge_base.retrieval_config.update", user.id, "knowledge_base", kb.id,
                 {"config": kb.retrieval_config})
    db.commit(); db.refresh(kb)
    return kb


@app.post("/api/v1/knowledge-bases/{kb_id}/activate")
def activate_kb(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    kb = db.get(KnowledgeBase, kb_id)
    if not kb or kb.deleted_at: raise HTTPException(404, "Knowledge base not found")
    kb.status = "active"; record_audit(db, "knowledge_base.activate", user.id, "knowledge_base", kb.id); db.commit(); return {"status": "success"}


@app.post("/api/v1/knowledge-bases/{kb_id}/disable")
def disable_kb(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    kb = db.get(KnowledgeBase, kb_id)
    if not kb or kb.deleted_at:
        raise HTTPException(404, "Knowledge base not found")
    kb.status = "disabled"
    record_audit(db, "knowledge_base.disable", user.id, "knowledge_base", kb.id)
    db.commit()
    return {"status": "success"}


@app.delete("/api/v1/knowledge-bases/{kb_id}")
def delete_kb(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    kb = db.get(KnowledgeBase, kb_id)
    if not kb or kb.deleted_at:
        raise HTTPException(404, "Knowledge base not found")
    if db.query(Document.id).filter_by(knowledge_base_id=kb.id).filter(Document.deleted_at.is_(None)).first():
        raise HTTPException(409, {"code": "KNOWLEDGE_BASE_NOT_EMPTY", "message": "Delete or move all documents before deleting this Knowledge Base.", "retryable": False})
    kb.deleted_at, kb.status = datetime.utcnow(), "deleted"
    record_audit(db, "knowledge_base.delete", user.id, "knowledge_base", kb.id)
    db.commit()
    return {"status": "deleted", "knowledge_base_id": kb.id}


@app.post("/api/v1/knowledge-bases/{kb_id}/documents")
def upload_document(kb_id: str, file: UploadFile = File(...), title: str | None = Form(None), document_type: str = Form("general"), published_at: date | None = Form(None), user: User = Depends(current_admin), db: Session = Depends(get_db)):
    kb = db.get(KnowledgeBase, kb_id)
    if not kb or kb.deleted_at: raise HTTPException(404, "Knowledge base not found")
    if kb.status == "disabled":
        raise HTTPException(409, {"code": "KNOWLEDGE_BASE_DISABLED", "message": "Activate this Knowledge Base before uploading documents.", "retryable": False})
    try: doc, job = create_document_job(db, kb_id, file, title, document_type, published_at)
    except ValueError as exc:
        status_code = 413 if str(exc) == "FILE_TOO_LARGE" else 400
        raise HTTPException(status_code, {"code": str(exc), "message": "Upload rejected", "retryable": False})
    record_audit(db, "document.upload", user.id, "document", doc.id, {"knowledge_base_id": kb_id, "filename": doc.original_filename, "document_type": doc.document_type}); db.commit()
    return {"status": "queued", "document_id": doc.id, "job_id": job.id, "document_type": doc.document_type, "legal_extraction_automatic": doc.document_type in {"legal", "regulation", "contract"}}


@app.get("/api/v1/knowledge-bases/{kb_id}/documents", response_model=list[DocumentOut])
def list_documents(kb_id: str, include_deleted: bool = False, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    rows = db.query(Document).filter_by(knowledge_base_id=kb_id)
    if not include_deleted:
        rows = rows.filter(Document.deleted_at.is_(None))
    return rows.order_by(Document.created_at.desc()).limit(200).all()


@app.post("/api/v1/knowledge-bases/{kb_id}/documents/reindex")
def reindex_embeddings(kb_id: str, force: bool = False, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
    count = queue_embedding_reindex(db, kb_id, force)
    record_audit(db, "document.embedding_reindex", user.id, "knowledge_base", kb_id, {"count": count, "force": force}); db.commit()
    return {"status": "queued", "count": count}


@app.get("/api/v1/documents/{document_id}/text")
def document_text(document_id: str, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if not doc: raise HTTPException(404, "Document not found")
    return {"document_id": doc.id, "status": doc.status, "document_type": doc.document_type, "text": doc.extracted_text, "error_code": doc.error_code, "legal_metadata": doc.legal_metadata}


@app.get("/api/v1/documents/{document_id}/jobs")
def document_jobs(document_id: str, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    if not db.get(Document, document_id):
        raise HTTPException(404, "Document not found")
    rows = db.query(ProcessingJob).filter_by(document_id=document_id).order_by(ProcessingJob.created_at.desc()).all()
    return [{"id": job.id, "type": job.job_type, "status": job.status, "stage": job.current_stage, "progress_percent": job.progress_percent,
             "attempt_count": job.attempt_count, "error_code": job.error_code, "error_message": job.error_message} for job in rows]


@app.post("/api/v1/documents/{document_id}/legal-extract")
def extract_legal_metadata(document_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if not doc or doc.deleted_at:
        raise HTTPException(404, "Document not found")
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
    if not doc or doc.deleted_at:
        raise HTTPException(404, "Document not found")
    doc.published_at = payload.published_at
    record_audit(db, "document.metadata.update", user.id, "document", doc.id, {"published_at": str(payload.published_at) if payload.published_at else None})
    db.commit()
    return {"status": "updated", "document_id": doc.id, "published_at": doc.published_at}


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
    if not doc or doc.deleted_at:
        raise HTTPException(404, "Document not found")
    doc.legal_metadata = None
    record_audit(db, "document.legal_metadata.delete", user.id, "document", doc.id)
    db.commit()
    return {"status": "deleted", "document_id": doc.id}


@app.post("/api/v1/documents/{document_id}/reprocess")
def reprocess_document(document_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
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
    if payload.document_id:
        document = db.get(Document, payload.document_id)
        if not document or document.knowledge_base_id != kb_id:
            raise HTTPException(400, "Document does not belong to the knowledge base")
    entity = create_entity(db, kb_id, payload)
    record_audit(db, "entity.create", user.id, "entity", entity.id, {"knowledge_base_id": kb_id}); db.commit()
    return entity


@app.get("/api/v1/knowledge-bases/{kb_id}/entities", response_model=list[EntityOut])
def list_entities(kb_id: str, search: str = "", _: User = Depends(current_admin), db: Session = Depends(get_db)):
    rows = db.query(Entity).filter_by(knowledge_base_id=kb_id).filter(Entity.deleted_at.is_(None))
    if search:
        rows = rows.filter(Entity.name.ilike(f"%{search}%"))
    return rows.order_by(Entity.name).limit(100).all()


@app.patch("/api/v1/entities/{entity_id}", response_model=EntityOut)
def update_entity(entity_id: str, payload: EntityUpdate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    entity = db.get(Entity, entity_id)
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
    if not entity or entity.deleted_at:
        raise HTTPException(404, "Entity not found")
    entity.deleted_at = datetime.utcnow()
    db.query(Relationship).filter((Relationship.source_entity_id == entity.id) | (Relationship.target_entity_id == entity.id), Relationship.deleted_at.is_(None)).update({"deleted_at": datetime.utcnow()}, synchronize_session=False)
    record_audit(db, "entity.delete", user.id, "entity", entity.id); db.commit()
    return {"status": "deleted", "entity_id": entity.id}


@app.post("/api/v1/knowledge-bases/{kb_id}/relationships", response_model=RelationshipOut)
def add_relationship(kb_id: str, payload: RelationshipCreate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    try:
        relationship = create_relationship(db, kb_id, payload)
        record_audit(db, "relationship.create", user.id, "relationship", relationship.id, {"knowledge_base_id": kb_id}); db.commit()
        return relationship
    except ValueError:
        raise HTTPException(400, "Entities must exist in the selected knowledge base")


@app.get("/api/v1/knowledge-bases/{kb_id}/relationships", response_model=list[RelationshipOut])
def list_relationships(kb_id: str, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    return db.query(Relationship).filter_by(knowledge_base_id=kb_id).filter(Relationship.deleted_at.is_(None)).limit(200).all()


@app.get("/api/v1/knowledge-bases/{kb_id}/legal-graph")
def get_legal_graph(kb_id: str, view: str = "verified", _: User = Depends(current_admin), db: Session = Depends(get_db)):
    if view not in {"verified", "suggested", "manual", "all"}:
        raise HTTPException(400, {"code": "LEGAL_GRAPH_VIEW_INVALID", "message": "view must be verified, suggested, manual, or all", "retryable": False})
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
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


@app.post("/api/v1/knowledge-bases/{kb_id}/legal-graph/rebuild")
def queue_legal_graph_rebuild(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
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
def legal_graph_rebuild_status(kb_id: str, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    job = db.query(ProcessingJob).filter_by(knowledge_base_id=kb_id, job_type="REBUILD_LEGAL_GRAPH").order_by(ProcessingJob.created_at.desc()).first()
    if not job:
        return {"status": "not_started", "job_id": None}
    return {"status": job.status, "job_id": job.id, "stage": job.current_stage, "progress_percent": job.progress_percent,
            "error_code": job.error_code, "error_message": job.error_message}


@app.patch("/api/v1/relationships/{relationship_id}/legal-review", response_model=RelationshipOut)
def review_legal_relationship(relationship_id: str, payload: LegalRelationshipReview, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    relationship = db.get(Relationship, relationship_id)
    if not relationship or relationship.deleted_at or not relationship.is_legal or relationship.origin != "ai_suggestion":
        raise HTTPException(404, "Suggested legal relationship not found")
    if relationship.review_status != "suggested":
        raise HTTPException(409, {"code": "LEGAL_RELATIONSHIP_ALREADY_REVIEWED", "message": "This legal suggestion has already been reviewed.", "retryable": False})
    relationship.review_status = payload.status
    relationship.attributes = {**(relationship.attributes or {}), "review_note": payload.note, "reviewed_by": user.username, "reviewed_at": datetime.utcnow().isoformat()}
    db.add(GraphProjectionEvent(event_type="relationship", relationship_id=relationship.id))
    record_audit(db, f"legal_graph.relationship.{payload.status}", user.id, "relationship", relationship.id, {"note": payload.note})
    db.commit(); db.refresh(relationship)
    return relationship


@app.post("/api/v1/knowledge-bases/{kb_id}/graph/sync")
def sync_knowledge_base_graph(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if not db.get(KnowledgeBase, kb_id):
        raise HTTPException(404, "Knowledge base not found")
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
    if not relationship or relationship.deleted_at:
        raise HTTPException(404, "Relationship not found")
    values = payload.model_dump(exclude_unset=True)
    for key, value in values.items(): setattr(relationship, key, value)
    db.add(GraphProjectionEvent(event_type="relationship", relationship_id=relationship.id)); record_audit(db, "relationship.update", user.id, "relationship", relationship.id, values); db.commit(); db.refresh(relationship)
    return relationship


@app.delete("/api/v1/relationships/{relationship_id}")
def delete_relationship(relationship_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    relationship = db.get(Relationship, relationship_id)
    if not relationship or relationship.deleted_at:
        raise HTTPException(404, "Relationship not found")
    relationship.deleted_at = datetime.utcnow(); record_audit(db, "relationship.delete", user.id, "relationship", relationship.id); db.commit()
    return {"status": "deleted", "relationship_id": relationship.id}


@app.get("/api/v1/knowledge-bases/{kb_id}/graph-layout")
def graph_layout(kb_id: str, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    rows = db.query(GraphNodeLayout).filter_by(knowledge_base_id=kb_id).all()
    return {"items": [{"entity_id": row.entity_id, "x": row.position_x, "y": row.position_y} for row in rows]}


@app.put("/api/v1/knowledge-bases/{kb_id}/graph-layout")
def save_graph_layout(kb_id: str, payload: GraphLayoutUpdate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
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
def get_entity_graph(entity_id: str, depth: int = 1, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404, "Entity not found")
    return entity_graph(db, entity, max(1, min(depth, 3)))


@app.get("/api/v1/entities/{entity_id}/sources")
def get_entity_sources(entity_id: str, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    rows = db.query(EntitySource, Document).join(Document, Document.id == EntitySource.document_id).filter(EntitySource.entity_id == entity_id).all()
    return {"entity_id": entity_id, "sources": [{"document_id": document.id, "title": document.title, "excerpt": source.excerpt} for source, document in rows]}


@app.post("/api/v1/query/impact")
def query_impact(payload: ImpactRequest, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    return analyze_impact(db, payload.subject, payload.knowledge_base_ids, payload.max_depth, payload.include_indirect)


@app.post("/api/v1/tokens", response_model=TokenCreated)
def create_token(payload: TokenCreate, user: User = Depends(current_admin), db: Session = Depends(get_db)):
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
    secret = create_token_secret()
    token = TokenKey(name=payload.name, description=payload.description, token_prefix=secret[:16], token_hash=token_digest(secret),
                     allowed_knowledge_base_ids=payload.allowed_knowledge_base_ids, allowed_tools=payload.allowed_tools,
                     expires_at=payload.expires_at, requests_per_minute=payload.requests_per_minute,
                     max_concurrent_requests=payload.max_concurrent_requests, query_timeout_seconds=payload.query_timeout_seconds)
    db.add(token); db.flush(); record_audit(db, "token.create", user.id, "token", token.id, {"name": token.name}); db.commit(); db.refresh(token)
    return {**TokenOut.model_validate(token).model_dump(), "token": secret}


@app.get("/api/v1/tokens", response_model=list[TokenOut])
def list_tokens(_: User = Depends(current_admin), db: Session = Depends(get_db)):
    return db.query(TokenKey).order_by(TokenKey.created_at.desc()).all()


@app.post("/api/v1/tokens/{token_id}/disable")
def disable_token(token_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    token = db.get(TokenKey, token_id)
    if not token:
        raise HTTPException(404, "Token not found")
    if token.status == "revoked":
        raise HTTPException(409, "A revoked token cannot be enabled or disabled")
    token.status = "inactive"
    record_audit(db, "token.disable", user.id, "token", token.id); db.commit()
    return {"status": "success"}


@app.post("/api/v1/tokens/{token_id}/enable")
def enable_token(token_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    token = db.get(TokenKey, token_id)
    if not token:
        raise HTTPException(404, "Token not found")
    if token.status == "revoked":
        raise HTTPException(409, "A revoked token cannot be enabled")
    token.status = "active"
    record_audit(db, "token.enable", user.id, "token", token.id); db.commit()
    return {"status": "success"}


@app.post("/api/v1/tokens/{token_id}/revoke")
def revoke_token(token_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    token = db.get(TokenKey, token_id)
    if not token:
        raise HTTPException(404, "Token not found")
    token.status = "revoked"
    token.revoked_at = datetime.utcnow()
    record_audit(db, "token.revoke", user.id, "token", token.id); db.commit()
    return {"status": "success"}


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


@app.get("/api/v1/audit-logs")
def list_audit_logs(limit: int = 100, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    rows = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(max(limit, 1), 500)).all()
    return [{"id": row.id, "action": row.action, "actor_user_id": row.actor_user_id, "target_type": row.target_type,
             "target_id": row.target_id, "metadata": row.metadata_json, "created_at": row.created_at} for row in rows]


@app.get("/api/v1/logs/transactions")
def list_request_transactions(limit: int = 100, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    """Return recent request transactions for the operator UI.

    Records contain only request metadata collected by the middleware. Request
    bodies, authorization headers, cookies, prompts, and token values are never
    persisted or returned here.
    """
    rows = db.query(AuditLog).filter(AuditLog.action == REQUEST_TRANSACTION_ACTION).order_by(
        AuditLog.created_at.desc()
    ).limit(min(max(limit, 1), 500)).all()
    request_ids = [(row.metadata_json or {}).get("request_id") or row.target_id for row in rows]
    execution_rows = db.query(AuditLog).filter(
        AuditLog.action == "retrieval.execution", AuditLog.target_id.in_([item for item in request_ids if item]),
    ).order_by(AuditLog.created_at.desc()).all() if request_ids else []
    executions = {row.target_id: row.metadata_json for row in execution_rows}
    return [{
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
        "created_at": row.created_at,
    }


@app.get("/api/v1/traces")
def list_retrieval_traces(limit: int = 100, transport: str | None = None, status: str | None = None,
                          search: str | None = None, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    """List safe RetrievalExecutor trace summaries for the Trace Explorer."""
    rows = db.query(AuditLog).filter(AuditLog.action == "retrieval.execution").order_by(
        AuditLog.created_at.desc()
    ).limit(min(max(limit, 1), 500)).all()
    needle = (search or "").strip().lower()
    summaries = []
    for row in rows:
        item = trace_summary(row)
        if transport and item["transport"] != transport:
            continue
        if status and item["status"] != status:
            continue
        if needle and needle not in " ".join(str(value) for value in (item["trace_id"], item["tool"], item["intent"], item["transport"])).lower():
            continue
        summaries.append(item)
    return summaries


@app.get("/api/v1/traces/{trace_id}")
def get_retrieval_trace(trace_id: str, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    """Return a root span and safe child spans for one retrieval execution."""
    row = db.query(AuditLog).filter(AuditLog.action == "retrieval.execution", AuditLog.target_id == trace_id).order_by(
        AuditLog.created_at.desc()
    ).first()
    if not row:
        raise HTTPException(404, "Trace not found")
    metadata = row.metadata_json or {}
    summary = trace_summary(row)
    return {**summary, "root_span": {"span_id": "root", "name": metadata.get("tool") or "knowledge query",
                                      "status": summary["status"], "duration_ms": summary["duration_ms"]},
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
    {"name": "find_entities", "description": "Find entities by name or alias", "inputSchema": {"type": "object", "properties": {"search_text": {"type": "string"}}, "required": ["search_text"]}},
    {"name": "analyze_relationships", "description": "Analyze entity relationships", "inputSchema": {"type": "object", "properties": {"subjects": {"type": "array"}, "question": {"type": "string"}}, "required": ["subjects", "question"]}},
    {"name": "analyze_impact", "description": "Analyze direct and indirect impact", "inputSchema": ImpactRequest.model_json_schema()},
    {"name": "get_sources", "description": "Retrieve sources for a result", "inputSchema": {"type": "object", "properties": {"result_id": {"type": "string"}}, "required": ["result_id"]}},
]


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
        elif method == "tools/list": result = {"tools": [tool for tool in MCP_TOOLS if not token.allowed_tools or tool["name"] in token.allowed_tools]}
        elif method == "tools/call":
            name = params.get("name"); arguments = params.get("arguments", {}); tool_name, tool_arguments = name, arguments
            authorize(token, name, list(token.allowed_knowledge_base_ids or []))
            effective_kb_ids = effective_mcp_knowledge_base_ids(db, token)
            if name == "search_knowledge":
                payload = QueryRequest.model_validate(arguments)
                payload.knowledge_base_ids = effective_kb_ids
                result = authorized_query(payload, token, db)
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
            else: return mcp_error(request_id, "MCP_TOOL_NOT_FOUND", "Tool not found")
            route = result.get("metadata", {}).get("retrieval_trace", [])
            record_retrieval_execution(db, request.state.request_id, result, transport="mcp", tool=name,
                                       rpc_request_id=str(request_id) if request_id is not None else None)
            record_mcp_tool_audit(db, token, request_id, name, arguments, route, mcp_started_at,
                                  retrieval_plan=result.get("metadata", {}).get("retrieval_plan"),
                                  effective_kb_ids=effective_kb_ids)
            result.setdefault("request_id", request_id)
            result = {"content": [{"type": "text", "text": result.get("answer", "Structured knowledge result available.")}], "structuredContent": result}
        else: return mcp_error(request_id, "MCP_METHOD_NOT_FOUND", "Method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "MCP_REQUEST_INVALID", "message": str(exc.detail)}
        if token is not None and tool_name:
            record_mcp_tool_audit(db, token, request_id, tool_name, tool_arguments, [], mcp_started_at, error_code=detail.get("code", "MCP_REQUEST_INVALID"))
        return mcp_error(request_id, detail.get("code", "MCP_REQUEST_INVALID"), detail.get("message", "Request rejected"), retryable=detail.get("retryable", False))
    except McpLimitExceeded as exc:
        if token is not None and tool_name:
            record_mcp_tool_audit(db, token, request_id, tool_name, tool_arguments, [], mcp_started_at, error_code=exc.code)
        return mcp_error(request_id, exc.code, exc.message, retryable=exc.code == "MCP_LIMIT_STORE_UNAVAILABLE")
    except RuntimeError as exc:
        if token is not None and tool_name:
            record_mcp_tool_audit(db, token, request_id, tool_name, tool_arguments, [], mcp_started_at, error_code=str(exc))
        if str(exc) == "MCP_TIMEOUT": return mcp_error(request_id, "MCP_TIMEOUT", "Token query timeout exceeded", retryable=True)
        return mcp_error(request_id, "MCP_EXECUTION_FAILED", "Tool execution failed", retryable=True)
    except Exception:
        if token is not None and tool_name:
            record_mcp_tool_audit(db, token, request_id, tool_name, tool_arguments, [], mcp_started_at, error_code="MCP_REQUEST_INVALID")
        return mcp_error(request_id, "MCP_REQUEST_INVALID", "Invalid MCP request")
    finally:
        if deadline_token is not None: reset_deadline(deadline_token)
        if acquired and token is not None: mcp_limiter.release(token)
