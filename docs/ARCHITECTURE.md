# Architecture

The API exposes REST and MCP interfaces but routes both into typed service contracts. Uploaded files are stored outside the web root and processed by a separate worker. `RetrievalEngine` is an adapter boundary: `LightRAGRetrievalEngine` is the only location permitted to know LightRAG details. PostgreSQL stores application records and query result cache; Redis is reserved for queue/cache integration; Neo4j is the production graph store.

The worker persists normalized, overlapping document chunks before it submits to LightRAG. PostgreSQL queries use a GIN-indexed `to_tsvector('simple', content)` full-text search as a deterministic, Knowledge-Base-scoped evidence layer; SQLite uses the same chunk contract with an `ILIKE` fallback for tests. When configured, the worker also uses LightRAG's asynchronous `/documents/text` endpoint and records its track ID. Queries fuse database evidence with LightRAG references, so the platform can still return citations when LightRAG is unavailable. Pin and test the server image/version before enabling it for production.

When the OpenRouter key is configured, the worker batches chunk text to OpenRouter's `/embeddings` endpoint and stores the returned 1536-dimensional vectors in pgvector. Semantic retrieval uses pgvector cosine distance and is fused with PostgreSQL FTS before LightRAG evidence. The configured embedding dimension must remain 1536 until a controlled migration/reindex is performed.

Set `RERANKER_ENABLED=true` and `OPENROUTER_RERANK_MODEL` to enable an optional OpenRouter cross-encoder rerank after fusion. If the reranker is unavailable, the platform returns the deterministic fused ranking instead. This setting does not enable LightRAG's own reranker.

For development, Docker Compose configures LightRAG's `openai` bindings against OpenRouter for both LLM and embedding traffic. The platform can verify credentials and configured model IDs through `POST /api/v1/system/test-openrouter`; it never stores the OpenRouter key in the database.

The initial Compose profile uses a shared LightRAG index. Because the pinned LightRAG server returns only the `file_source` basename in references, the adapter writes immutable Knowledge-Base and document IDs into that basename, decodes them on retrieval, and never returns cross-KB evidence. Dedicated LightRAG instances/workspaces remain the production scaling path for retrieval quality isolation. Reranking remains disabled unless a compatible reranker is explicitly configured.

Entities, relationships, and their document excerpts are persisted by the platform as its source-of-truth graph contract. LightRAG enriches retrieval, but Graph and Impact APIs never expose LightRAG-specific identifiers. Impact traversal is bounded by the caller's depth, currently capped at three hops, and each result returns the relationship-source citations that support it.

When `NEO4J_HTTP_URL` and `NEO4J_PASSWORD` are configured, entity and relationship writes are projected to Neo4j through parameterized Cypher. The projection is best-effort: PostgreSQL remains authoritative and a transient Neo4j failure cannot discard an accepted administration change. A durable projection outbox and Neo4j-backed traversal are the next graph-hardening increment.

Graph writes now create a transactional `graph_projection_events` outbox row alongside the PostgreSQL entity or relationship. The worker delivers one event at a time, tracks attempts, and retries transient Neo4j failures with bounded exponential backoff. The projection status is available to an authenticated administrator at `GET /api/v1/system/graph-projection`.
