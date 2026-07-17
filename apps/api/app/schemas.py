from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .legal_registry import VALID_KINDS
from .planner import RetrievalPolicy


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=256)


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # Optional for UI/API callers.  The service derives a collision-safe code
    # from the display name when omitted, including for non-Latin names.
    code: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{1,118}[a-z0-9]$")
    description: str | None = None
    default_language: str = "auto"
    entity_schema: dict[str, Any] = Field(default_factory=dict)
    relationship_schema: dict[str, Any] = Field(default_factory=dict)


class KnowledgeBaseOut(ORMModel):
    id: str
    code: str
    name: str
    description: str | None
    default_language: str
    status: str
    retrieval_config: dict[str, Any]


class RetrievalConfigUpdate(BaseModel):
    retrieval_mode: str | None = None
    enable_vector: bool | None = None
    enable_fulltext: bool | None = None
    enable_graph: bool | None = None
    enable_lightrag: bool | None = None
    enable_reranker: bool | None = None
    planner_llm_fallback: bool | None = None
    default_top_k: int | None = Field(default=None, ge=1, le=30)
    maximum_top_k: int | None = Field(default=None, ge=1, le=50)
    maximum_graph_depth: int | None = Field(default=None, ge=1, le=3)
    citation_required: bool | None = None
    legal_awareness: bool | None = None
    exclude_invalid: bool | None = None
    authority_weight: float | None = Field(default=None, ge=0, le=1)
    recency_weight: float | None = Field(default=None, ge=0, le=1)
    status_weight: float | None = Field(default=None, ge=0, le=1)

    def merged(self, current: dict[str, Any]) -> dict[str, Any]:
        values = {key: value for key, value in self.model_dump().items() if value is not None}
        return RetrievalPolicy.model_validate({**current, **values}).model_dump()


class DocumentOut(ORMModel):
    id: str
    knowledge_base_id: str
    original_filename: str
    title: str | None
    document_type: str
    published_at: date | None
    mime_type: str
    file_size: int
    status: str
    error_code: str | None
    error_message: str | None
    legal_metadata: dict[str, Any] | None
    indexed_at: datetime | None
    deleted_at: datetime | None
    # Latest job fields let the Documents UI observe follow-up work such as
    # legal metadata extraction even after the document itself is searchable.
    processing_job_status: str | None = None
    processing_job_type: str | None = None
    processing_job_stage: str | None = None


class LegalMetadataUpdate(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentMetadataUpdate(BaseModel):
    published_at: date | None = None


class QueryFilters(BaseModel):
    published_from: date | None = None
    published_to: date | None = None
    as_of_date: date | None = None
    include_historical: bool = False


class LegalInstrumentOut(ORMModel):
    id: str
    knowledge_base_id: str
    document_id: str
    family_id: str | None
    kind: str
    authority_level: int
    official_title: str | None
    official_number: str | None
    issuer: str | None
    jurisdiction: str | None
    version_label: str | None
    enacted_year: int | None
    effective_from: date | None
    effective_to: date | None
    status: str
    status_source: str
    status_reason: str | None
    review_status: str
    source_uri: str | None
    source_reference: str | None
    legal_work_key: str | None
    document_class: str | None
    version_date: date | None
    reviewed_at: datetime | None
    reviewed_by: str | None


class LegalInstrumentUpdate(BaseModel):
    kind: str | None = Field(default=None, pattern="^(" + "|".join(VALID_KINDS) + ")$")
    authority_level: int | None = Field(default=None, ge=0, le=100)
    effective_from: date | None = None
    effective_to: date | None = None
    status: str | None = Field(default=None, pattern="^(in_force|amended|superseded|repealed|not_yet_effective|unknown)$")
    family_id: str | None = None
    version_label: str | None = Field(default=None, max_length=120)
    source_uri: str | None = Field(default=None, max_length=2000)
    source_reference: str | None = Field(default=None, max_length=500)


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    allowed_knowledge_base_ids: list[str] = Field(default_factory=list, max_length=100)
    allowed_tools: list[str] = Field(default_factory=list, max_length=10)
    expires_at: datetime | None = None
    requests_per_minute: int = Field(default=60, ge=1, le=10000)
    max_concurrent_requests: int = Field(default=5, ge=1, le=100)
    query_timeout_seconds: int = Field(default=60, ge=1, le=300)


class TokenOut(ORMModel):
    id: str
    name: str
    token_prefix: str
    allowed_knowledge_base_ids: list[str]
    allowed_tools: list[str]
    status: str
    expires_at: datetime | None
    requests_per_minute: int
    max_concurrent_requests: int
    query_timeout_seconds: int


class TokenCreated(TokenOut):
    token: str


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=100)
    filters: QueryFilters = Field(default_factory=QueryFilters)
    response_mode: Literal["evidence", "answer", "both"] = "both"
    max_sources: int = Field(default=10, ge=1, le=20)
    language: str = "auto"


class ImpactRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    scenario: str = Field(min_length=1, max_length=1000)
    entity_id: str | None = None
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=100)
    include_indirect: bool = True
    max_depth: int = Field(default=2, ge=1, le=3)


class EntityCreate(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    entity_type: str = Field(default="Concept", min_length=1, max_length=100)
    description: str | None = None
    aliases: list[str] = Field(default_factory=list, max_length=50)
    attributes: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0, le=1)
    document_id: str | None = None
    excerpt: str | None = Field(default=None, max_length=5000)


class EntityOut(ORMModel):
    id: str
    knowledge_base_id: str
    name: str
    canonical_name: str
    identity_key: str
    entity_type: str
    description: str | None
    aliases: list[str]
    confidence: float | None
    origin: str
    review_status: str
    is_legal: bool
    source_count: int
    attributes: dict[str, Any] = Field(default_factory=dict)


class EntityUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    entity_type: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=5000)
    aliases: list[str] | None = Field(default=None, max_length=50)
    attributes: dict[str, Any] | None = None


class RelationshipCreate(BaseModel):
    source_entity_id: str
    target_entity_id: str
    relationship_type: str = Field(min_length=1, max_length=100)
    description: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    document_id: str | None = None
    excerpt: str | None = Field(default=None, max_length=5000)


class RelationshipOut(ORMModel):
    id: str
    knowledge_base_id: str
    source_entity_id: str
    target_entity_id: str
    relationship_type: str
    description: str | None
    confidence: float | None
    origin: str
    review_status: str
    is_legal: bool
    source_count: int
    attributes: dict[str, Any] = Field(default_factory=dict)


class RelationshipUpdate(BaseModel):
    relationship_type: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=5000)
    confidence: float | None = Field(default=None, ge=0, le=1)


class LegalRelationshipReview(BaseModel):
    status: str = Field(pattern="^(verified|rejected)$")
    note: str | None = Field(default=None, max_length=2000)


class GraphLayoutItem(BaseModel):
    entity_id: str
    x: float = Field(ge=-100_000, le=100_000)
    y: float = Field(ge=-100_000, le=100_000)


class GraphLayoutUpdate(BaseModel):
    items: list[GraphLayoutItem] = Field(max_length=500)


class QueryFeedbackCreate(BaseModel):
    rating: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=2000)
