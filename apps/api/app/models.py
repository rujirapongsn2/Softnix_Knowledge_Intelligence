import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON
from pgvector.sqlalchemy import Vector

from .db import Base


def uuid4() -> str:
    return str(uuid.uuid4())


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


ROLE_USER = "user"
ROLE_MANAGER = "manager"
ROLE_ADMIN = "admin"
ROLES = (ROLE_USER, ROLE_MANAGER, ROLE_ADMIN)


class User(Timestamped, Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(500))
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # RBAC: 'user' | 'manager' | 'admin'.  Bootstrap admin gets 'admin' from
    # migration 0027 / bootstrap(); every other account starts as 'user'.
    role: Mapped[str] = mapped_column(String(20), default=ROLE_USER, server_default=ROLE_USER, index=True)
    # v1: single group per user (see plan — junction table only if multi-group
    # is ever needed).  NULL = unassigned (visible to admins only).
    group_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("groups.id"), nullable=True)
    credentials_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")


class Group(Timestamped, Base):
    __tablename__ = "groups"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class KbOwner(Base):
    """Many-to-many Knowledge Base ↔ owner.

    v1 writes exactly one row per KB (the creator).  The table is already
    many-to-many so sharing a KB later is an INSERT, not a migration.
    """
    __tablename__ = "kb_owners"
    kb_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_bases.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True, index=True)


class KnowledgeBase(Timestamped, Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A small, allow-listed UI key.  The browser maps it to its own inline SVG
    # rather than accepting user-supplied markup or external image URLs.
    icon: Mapped[str] = mapped_column(String(40), default="auto")
    default_language: Mapped[str] = mapped_column(String(20), default="auto")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    retrieval_config: Mapped[dict] = mapped_column(JSON, default=dict)
    entity_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    relationship_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DocumentMetadataTemplate(Timestamped, Base):
    """A knowledge-base scoped, versioned form used to describe a document.

    ``base_document_type`` deliberately stays within the small set of processing
    profiles.  A curator may create a type called "ประกาศ" without creating an
    unreviewed processing path or changing the legal pipeline.
    """
    __tablename__ = "document_metadata_templates"
    __table_args__ = (UniqueConstraint("knowledge_base_id", "code", name="uq_document_metadata_template_code"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    code: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_document_type: Mapped[str] = mapped_column(String(40), default="general", index=True)
    fields: Mapped[list] = mapped_column(JSON, default=list)
    # Nullable preserves rows created before profile inheritance was explicit.
    # New rows store only administrator-defined fields here.
    custom_fields: Mapped[list | None] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


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
    # The selected form is snapshotted on the document so changing a template
    # later never makes historical metadata ambiguous.
    metadata_template_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    metadata_template_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_template_fields: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    document_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    metadata_search_text: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class DocumentMetadataValue(Base):
    """Indexed, filterable metadata projection for query-time exact filters."""
    __tablename__ = "document_metadata_values"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    field_key: Mapped[str] = mapped_column(String(80), index=True)
    value_text: Mapped[str] = mapped_column(String(10000))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("document_id", "field_key", name="uq_document_metadata_value"),
        Index("ix_document_metadata_filter", "knowledge_base_id", "field_key", "value_text"),
    )


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
    # Populated only for legal documents by the section-aware splitter; None for
    # fixed-size chunks of general documents.
    section_kind: Mapped[str | None] = mapped_column(String(30), nullable=True)
    section_number: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    section_label: Mapped[str | None] = mapped_column(String(200), nullable=True)


class LegalFamily(Timestamped, Base):
    __tablename__ = "legal_families"
    __table_args__ = (UniqueConstraint("knowledge_base_id", "normalized_key", name="uq_legal_family_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    base_title: Mapped[str] = mapped_column(String(500))
    normalized_key: Mapped[str] = mapped_column(String(700), index=True)


class LegalInstrument(Timestamped, Base):
    __tablename__ = "legal_instruments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), unique=True, index=True)
    family_id: Mapped[str | None] = mapped_column(ForeignKey("legal_families.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(40), default="other", index=True)
    authority_level: Mapped[int] = mapped_column(Integer, default=20)
    official_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    official_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    issuer: Mapped[str | None] = mapped_column(String(300), nullable=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(120), nullable=True)
    version_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    enacted_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    # in_force | amended | superseded | repealed | not_yet_effective | unknown
    status: Mapped[str] = mapped_column(String(20), default="unknown", index=True)
    status_source: Mapped[str] = mapped_column(String(20), default="resolver")
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(String(20), default="unreviewed")
    # Curator-facing provenance.  These fields are intentionally nullable so
    # legacy legal metadata can be migrated without inventing an authority.
    source_uri: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    source_reference: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Stable identity for a legal work across main, consolidated and amendment
    # expressions.  These fields are populated from the official corpus header.
    legal_work_key: Mapped[str | None] = mapped_column(String(700), nullable=True, index=True)
    document_class: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    # ``latest_consolidated`` is deliberately a role rather than a status: a
    # publisher can designate a latest compilation while its legal status is
    # still resolved independently from dates and amendment relations.
    version_role: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    version_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class LegalInstrumentRelation(Timestamped, Base):
    __tablename__ = "legal_instrument_relations"
    __table_args__ = (
        UniqueConstraint("source_instrument_id", "relation", "target_instrument_id", "target_provision",
                         name="uq_legal_instrument_relation"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    source_instrument_id: Mapped[str] = mapped_column(ForeignKey("legal_instruments.id"), index=True)
    target_instrument_id: Mapped[str | None] = mapped_column(ForeignKey("legal_instruments.id"), nullable=True, index=True)
    relationship_id: Mapped[str | None] = mapped_column(ForeignKey("relationships.id"), nullable=True, index=True)
    target_text: Mapped[str | None] = mapped_column(String(700), nullable=True)
    target_provision: Mapped[str | None] = mapped_column(String(120), nullable=True)
    relation: Mapped[str] = mapped_column(String(30), index=True)
    evidence_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    origin: Mapped[str] = mapped_column(String(30), default="legal_schema")
    review_status: Mapped[str] = mapped_column(String(20), default="suggested", index=True)


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


class TraceRun(Base):
    """Normalized, query-friendly retrieval trace root.

    AuditLog remains the immutable compliance record and compatibility source;
    this table is the hot observability index used by Trace Explorer.
    """
    __tablename__ = "trace_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    transport: Mapped[str] = mapped_column(String(30), default="api", index=True)
    tool: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    trace_status: Mapped[str] = mapped_column(String(20), default="success", index=True)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    knowledge_base_ids: Mapped[list] = mapped_column(JSON, default=list)
    retrieval_plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    request_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    response_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class TraceSpan(Base):
    __tablename__ = "trace_spans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4)
    trace_id: Mapped[str] = mapped_column(ForeignKey("trace_runs.id"), index=True)
    span_id: Mapped[str] = mapped_column(String(100), index=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    channel: Mapped[str] = mapped_column(String(80), index=True)
    system: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    offset_ms: Mapped[int] = mapped_column(Integer, default=0)
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    input_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    output_summary: Mapped[dict] = mapped_column(JSON, default=dict)
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
    allowed_scopes: Mapped[list] = mapped_column(JSON, default=list)
    # The Knowledge Base a documents:write token may ingest into. Kept separate
    # from allowed_knowledge_base_ids (the MCP read axis) so one token's read
    # scope and write scope never have to match.
    allowed_ingest_knowledge_base_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    requests_per_minute: Mapped[int] = mapped_column(Integer, default=60)
    max_concurrent_requests: Mapped[int] = mapped_column(Integer, default=5)
    query_timeout_seconds: Mapped[int] = mapped_column(Integer, default=60)
    status: Mapped[str] = mapped_column(String(20), default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # RBAC: who issued this token.  Legacy rows are backfilled to the bootstrap
    # admin by migration 0027; visibility rules use this (creator, or creator's
    # group for managers) — never a wildcard.
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)


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
