from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=256)


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,118}[a-z0-9]$")
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


class DocumentOut(ORMModel):
    id: str
    knowledge_base_id: str
    original_filename: str
    title: str | None
    document_type: str
    mime_type: str
    file_size: int
    status: str
    error_code: str | None
    error_message: str | None
    legal_metadata: dict[str, Any] | None
    indexed_at: datetime | None
    deleted_at: datetime | None


class LegalMetadataUpdate(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    allowed_knowledge_base_ids: list[str] = Field(default_factory=list, max_length=100)
    allowed_tools: list[str] = Field(default_factory=list, max_length=5)
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
    filters: dict[str, Any] = Field(default_factory=dict)
    response_mode: Literal["evidence", "answer", "both"] = "both"
    max_sources: int = Field(default=10, ge=1, le=20)
    language: str = "auto"


class ImpactRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    scenario: str = Field(min_length=1, max_length=1000)
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
    entity_type: str
    description: str | None
    aliases: list[str]
    confidence: float | None
    source_count: int


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
    source_count: int


class RelationshipUpdate(BaseModel):
    relationship_type: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=5000)
    confidence: float | None = Field(default=None, ge=0, le=1)


class GraphLayoutItem(BaseModel):
    entity_id: str
    x: float = Field(ge=-100_000, le=100_000)
    y: float = Field(ge=-100_000, le=100_000)


class GraphLayoutUpdate(BaseModel):
    items: list[GraphLayoutItem] = Field(max_length=500)


class QueryFeedbackCreate(BaseModel):
    rating: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=2000)
