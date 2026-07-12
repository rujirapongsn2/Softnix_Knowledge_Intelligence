from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
import httpx
import redis
from sqlalchemy.orm import Session

from .config import get_settings
from .audit import record_audit
from .db import Base, engine, get_db
from .external_ocr import ExternalOcrClient
from .graph_store import Neo4jGraphStore
from .models import AuditLog, Document, Entity, EntitySource, GraphNodeLayout, GraphProjectionEvent, KnowledgeBase, ProcessingJob, QueryFeedback, QueryResult, Relationship, TokenKey, User
from .observability import metrics, now
from .openrouter import OpenRouterClient
from .mcp_limits import McpLimitExceeded, mcp_limiter
from .request_budget import reset_deadline, set_deadline
from .schemas import DocumentOut, EntityCreate, EntityOut, EntityUpdate, GraphLayoutUpdate, ImpactRequest, KnowledgeBaseCreate, KnowledgeBaseOut, LegalMetadataUpdate, LoginRequest, QueryFeedbackCreate, QueryRequest, RelationshipCreate, RelationshipOut, RelationshipUpdate, TokenCreate, TokenCreated, TokenOut
from .security import authorize, bearer_token, create_session_token, create_token_secret, current_admin, password_hash, refresh_admin, token_digest, verify_password
from .services import DEFAULT_RETRIEVAL_CONFIG, analyze_impact, build_query_result, create_document_job, create_entity, create_relationship, entity_graph, process_next_job, queue_embedding_reindex, resolve_entity, sync_lightrag_document_graph

app = FastAPI(title="Softnix Knowledge Intelligence Platform", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:8080", "http://localhost:8081"], allow_credentials=True,
                   allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type"])


@app.middleware("http")
async def request_observability(request: Request, call_next):
    started = now()
    request_id = request.headers.get("X-Request-ID") or __import__("uuid").uuid4().hex
    try:
        response = await call_next(request)
    except Exception:
        metrics.observe(request.method, request.url.path, 500, now() - started)
        raise
    metrics.observe(request.method, request.url.path, response.status_code, now() - started)
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


@app.post("/api/v1/knowledge-bases/{kb_id}/activate")
def activate_kb(kb_id: str, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    kb = db.get(KnowledgeBase, kb_id)
    if not kb: raise HTTPException(404, "Knowledge base not found")
    kb.status = "active"; record_audit(db, "knowledge_base.activate", user.id, "knowledge_base", kb.id); db.commit(); return {"status": "success"}


@app.post("/api/v1/knowledge-bases/{kb_id}/documents")
def upload_document(kb_id: str, file: UploadFile = File(...), title: str | None = Form(None), document_type: str = Form("general"), user: User = Depends(current_admin), db: Session = Depends(get_db)):
    if not db.get(KnowledgeBase, kb_id): raise HTTPException(404, "Knowledge base not found")
    try: doc, job = create_document_job(db, kb_id, file, title, document_type)
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
    return build_query_result(db, payload.query, kb_ids, payload.max_sources, token.id if token else None)


@app.post("/api/v1/query")
def admin_query(payload: QueryRequest, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    return authorized_query(payload, None, db)


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


MCP_TOOLS = [
    {"name": "search_knowledge", "description": "Search knowledge bases with automatic retrieval planning", "inputSchema": QueryRequest.model_json_schema()},
    {"name": "find_entities", "description": "Find entities by name or alias", "inputSchema": {"type": "object", "properties": {"search_text": {"type": "string"}}, "required": ["search_text"]}},
    {"name": "analyze_relationships", "description": "Analyze entity relationships", "inputSchema": {"type": "object", "properties": {"subjects": {"type": "array"}, "question": {"type": "string"}}, "required": ["subjects", "question"]}},
    {"name": "analyze_impact", "description": "Analyze direct and indirect impact", "inputSchema": ImpactRequest.model_json_schema()},
    {"name": "get_sources", "description": "Retrieve sources for a result", "inputSchema": {"type": "object", "properties": {"result_id": {"type": "string"}}, "required": ["result_id"]}},
]


def mcp_error(request_id: Any, code: str, message: str, *, retryable: bool = False):
    return JSONResponse(status_code=200, content={"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message, "retryable": retryable}})


@app.post("/mcp")
async def mcp(request: Request, db: Session = Depends(get_db)):
    request_id = None
    token = None
    deadline_token = None
    acquired = False
    try:
        body: dict[str, Any] = await request.json(); request_id = body.get("id")
        token = bearer_token(request, db); mcp_limiter.acquire(token); acquired = True
        deadline_token = set_deadline(token.query_timeout_seconds)
        method, params = body.get("method"), body.get("params", {})
        if method == "initialize": result = {"protocolVersion": "2025-03-26", "serverInfo": {"name": "softnix-knowledge", "version": "0.1.0"}, "capabilities": {"tools": {}}}
        elif method == "tools/list": result = {"tools": [tool for tool in MCP_TOOLS if not token.allowed_tools or tool["name"] in token.allowed_tools]}
        elif method == "tools/call":
            name = params.get("name"); arguments = params.get("arguments", {}); authorize(token, name, arguments.get("knowledge_base_ids", token.allowed_knowledge_base_ids))
            if name == "search_knowledge": result = authorized_query(QueryRequest.model_validate(arguments), token, db)
            elif name == "find_entities":
                kb_ids = arguments.get("knowledge_base_ids") or token.allowed_knowledge_base_ids
                rows = db.query(Entity).filter(Entity.knowledge_base_id.in_(kb_ids), Entity.deleted_at.is_(None), Entity.name.ilike(f"%{arguments.get('search_text', '')}%")).limit(min(arguments.get("limit", 10), 50)).all()
                result = {"status": "success", "entities": [EntityOut.model_validate(row).model_dump() for row in rows]}
            elif name == "analyze_relationships":
                kb_ids = arguments.get("knowledge_base_ids") or token.allowed_knowledge_base_ids
                entity = resolve_entity(db, kb_ids, arguments.get("subjects", [""])[0])
                result = {"status": "success", "graph": entity_graph(db, entity, min(arguments.get("max_depth", 1), 3)) if entity else {"nodes": [], "edges": []}}
            elif name == "analyze_impact":
                impact = ImpactRequest.model_validate(arguments)
                kb_ids = impact.knowledge_base_ids or token.allowed_knowledge_base_ids
                result = analyze_impact(db, impact.subject, kb_ids, impact.max_depth, impact.include_indirect)
            elif name == "get_sources":
                saved = db.get(QueryResult, arguments.get("result_id"))
                if saved: authorize(token, "get_sources", saved.result_json.get("metadata", {}).get("knowledge_base_ids", []))
                result = {"sources": saved.result_json.get("sources", [])} if saved else {"sources": []}
            else: return mcp_error(request_id, "MCP_TOOL_NOT_FOUND", "Tool not found")
            result.setdefault("request_id", request_id)
            result = {"content": [{"type": "text", "text": result.get("answer", "Structured knowledge result available.")}], "structuredContent": result}
        else: return mcp_error(request_id, "MCP_METHOD_NOT_FOUND", "Method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "MCP_REQUEST_INVALID", "message": str(exc.detail)}
        return mcp_error(request_id, detail.get("code", "MCP_REQUEST_INVALID"), detail.get("message", "Request rejected"), retryable=detail.get("retryable", False))
    except McpLimitExceeded as exc:
        return mcp_error(request_id, exc.code, exc.message, retryable=exc.code == "MCP_LIMIT_STORE_UNAVAILABLE")
    except RuntimeError as exc:
        if str(exc) == "MCP_TIMEOUT": return mcp_error(request_id, "MCP_TIMEOUT", "Token query timeout exceeded", retryable=True)
        return mcp_error(request_id, "MCP_EXECUTION_FAILED", "Tool execution failed", retryable=True)
    except Exception:
        return mcp_error(request_id, "MCP_REQUEST_INVALID", "Invalid MCP request")
    finally:
        if deadline_token is not None: reset_deadline(deadline_token)
        if acquired and token is not None: mcp_limiter.release(token)
