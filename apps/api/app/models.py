import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON
from pgvector.sqlalchemy import Vector

from .db import Base


def uuid4() -> str:
    return str(uuid.uuid4())


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class User(Timestamped, Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(500))
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class KnowledgeBase(Timestamped, Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_language: Mapped[str] = mapped_column(String(20), default="auto")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    retrieval_config: Mapped[dict] = mapped_column(JSON, default=dict)
    entity_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    relationship_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Document(Timestamped, Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("knowledge_base_id", "checksum_sha256", name="uq_document_checksum"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    original_filename: Mapped[str] = mapped_column(String(500))
    stored_filename: Mapped[str] = mapped_column(String(500))
    storage_path: Mapped[str] = mapped_column(String(1000))
    mime_type: Mapped[str] = mapped_column(String(150))
    file_size: Mapped[int] = mapped_column(Integer)
    checksum_sha256: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # This is an authoring choice, not a MIME-type inference.  It controls
    # post-processing such as the legal metadata extraction workflow.
    document_type: Mapped[str] = mapped_column(String(40), default="general", index=True)
    published_at: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    legal_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    external_engine_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class DocumentChunk(Timestamped, Base):
    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_document_chunk_index"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    token_count: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536).with_variant(JSON, "sqlite"), nullable=True)


class ProcessingJob(Timestamped, Base):
    __tablename__ = "processing_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    knowledge_base_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_bases.id"), nullable=True)
    job_type: Mapped[str] = mapped_column(String(50), default="PROCESS_DOCUMENT")
    status: Mapped[str] = mapped_column(String(30), default="queued")
    current_stage: Mapped[str | None] = mapped_column(String(50), nullable=True)
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class Entity(Timestamped, Base):
    __tablename__ = "entities"
    __table_args__ = (UniqueConstraint("knowledge_base_id", "identity_key", "entity_type", name="uq_entity_identity"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    canonical_name: Mapped[str] = mapped_column(String(500), index=True)
    # Legal provisions are document-scoped.  A separate identity key prevents
    # "มาตรา 1" from different instruments being merged into one entity.
    identity_key: Mapped[str] = mapped_column(String(700), index=True)
    entity_type: Mapped[str] = mapped_column(String(100), index=True, default="Concept")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    origin: Mapped[str] = mapped_column(String(30), default="manual", index=True)
    review_status: Mapped[str] = mapped_column(String(20), default="verified", index=True)
    is_legal: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Relationship(Timestamped, Base):
    __tablename__ = "relationships"
    __table_args__ = (UniqueConstraint("knowledge_base_id", "source_entity_id", "target_entity_id", "relationship_type", name="uq_relationship_edge"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    source_entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    target_entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    relationship_type: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    origin: Mapped[str] = mapped_column(String(30), default="manual", index=True)
    review_status: Mapped[str] = mapped_column(String(20), default="verified", index=True)
    is_legal: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GraphNodeLayout(Timestamped, Base):
    __tablename__ = "graph_node_layouts"
    __table_args__ = (UniqueConstraint("knowledge_base_id", "entity_id", name="uq_graph_node_layout"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    position_x: Mapped[float] = mapped_column(Float, default=0)
    position_y: Mapped[float] = mapped_column(Float, default=0)


class EntitySource(Base):
    __tablename__ = "entity_sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    excerpt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RelationshipSource(Base):
    __tablename__ = "relationship_sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    relationship_id: Mapped[str] = mapped_column(ForeignKey("relationships.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    excerpt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GraphProjectionEvent(Timestamped, Base):
    __tablename__ = "graph_projection_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    event_type: Mapped[str] = mapped_column(String(30), index=True)
    entity_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True, index=True)
    relationship_id: Mapped[str | None] = mapped_column(ForeignKey("relationships.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class TokenKey(Timestamped, Base):
    __tablename__ = "token_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_prefix: Mapped[str] = mapped_column(String(40), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    allowed_knowledge_base_ids: Mapped[list] = mapped_column(JSON, default=list)
    allowed_tools: Mapped[list] = mapped_column(JSON, default=list)
    requests_per_minute: Mapped[int] = mapped_column(Integer, default=60)
    max_concurrent_requests: Mapped[int] = mapped_column(Integer, default=5)
    query_timeout_seconds: Mapped[int] = mapped_column(Integer, default=60)
    status: Mapped[str] = mapped_column(String(20), default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class QueryResult(Base):
    __tablename__ = "query_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    token_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class QueryFeedback(Base):
    __tablename__ = "query_feedback"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    result_id: Mapped[str] = mapped_column(ForeignKey("query_results.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
