# Architecture

The API exposes REST and MCP interfaces but routes both into typed service contracts. Uploaded files are stored outside the web root and processed by a separate worker. `RetrievalEngine` is an adapter boundary: `LightRAGRetrievalEngine` is the only location permitted to know LightRAG details. PostgreSQL stores application records and query result cache; Redis is reserved for queue/cache integration; Neo4j is the production graph store.

The worker persists normalized, overlapping document chunks before it submits to LightRAG. PostgreSQL queries use a GIN-indexed `to_tsvector('simple', content)` full-text search as a deterministic, Knowledge-Base-scoped evidence layer; SQLite uses the same chunk contract with an `ILIKE` fallback for tests. When configured, the worker also uses LightRAG's asynchronous `/documents/text` endpoint and records its track ID. The Auto Retrieval Planner selects permitted channels and the executor runs independent channels in parallel, then fuses their evidence; a failed channel does not remove citations from the other channels. Pin and test the server image/version before enabling it for production.

When the OpenRouter key is configured, the worker batches chunk text to OpenRouter's `/embeddings` endpoint and stores the returned 1536-dimensional vectors in pgvector. Semantic retrieval uses pgvector cosine distance and is fused with PostgreSQL FTS before LightRAG evidence. The configured embedding dimension must remain 1536 until a controlled migration/reindex is performed.

Set `RERANKER_ENABLED=true` and `OPENROUTER_RERANK_MODEL` to enable an optional OpenRouter cross-encoder rerank after fusion. If the reranker is unavailable, the platform returns the deterministic fused ranking instead. This setting does not enable LightRAG's own reranker.

For development, Docker Compose configures LightRAG's `openai` bindings against OpenRouter for both LLM and embedding traffic. The platform can verify credentials and configured model IDs through `POST /api/v1/system/test-openrouter`; it never stores the OpenRouter key in the database.

The initial Compose profile uses a shared LightRAG index. Because the pinned LightRAG server returns only the `file_source` basename in references, the adapter writes immutable Knowledge-Base and document IDs into that basename, decodes them on retrieval, and never returns cross-KB evidence. Dedicated LightRAG instances/workspaces remain the production scaling path for retrieval quality isolation. Reranking remains disabled unless a compatible reranker is explicitly configured.

Entities, relationships, and their document excerpts are persisted by the platform as its source-of-truth graph contract. The planner's graph channel uses Neo4j as a bounded ID accelerator when available, then resolves relationship evidence and citations from PostgreSQL. It falls back to PostgreSQL traversal when Neo4j is unavailable, lagging, or missing evidence. LightRAG enriches retrieval, but Graph and Impact APIs never expose LightRAG-specific identifiers. Impact traversal is bounded by the caller's depth, currently capped at three hops, and each result returns the relationship-source citations that support it.

When `NEO4J_HTTP_URL` and `NEO4J_PASSWORD` are configured, entity and relationship writes are projected to Neo4j through parameterized Cypher, including origin, review status, and legal flags. The projection is best-effort: PostgreSQL remains authoritative and a transient Neo4j failure cannot discard an accepted administration change. The planner treats Neo4j as an accelerator, never as the provenance authority.

Graph writes now create a transactional `graph_projection_events` outbox row alongside the PostgreSQL entity or relationship. The worker delivers one event at a time, tracks attempts, and retries transient Neo4j failures with bounded exponential backoff. The projection status is available to an authenticated administrator at `GET /api/v1/system/graph-projection`.

## Legal registry and temporal/relationship-aware retrieval

Legal Graph Schema v2 extraction populates `documents.legal_metadata`, but a `legal_metadata` blob alone cannot answer "which version is current." A deterministic legal registry sits alongside the existing legal graph:

- `legal_families` groups an instrument with its amendments under one canonical title (`app/legal_registry.py:normalize_family_key`), independent of `(ฉบับที่ N)` suffixes and พ.ศ./ค.ศ. year notation.
- `legal_instruments` (one row per legal/regulation/contract document) carries a rule-based authority level (`app/legal_registry.py:AUTHORITY_LEVELS`, รัฐธรรมนูญ=100 down to FAQ=20), effective_from/effective_to, and a status (`in_force`, `amended`, `superseded`, `repealed`, `not_yet_effective`, `unknown`).
- `legal_instrument_relations` mirrors the reviewed AMENDS/REPEALS/SUPERSEDES/ISSUED_UNDER/IMPLEMENTS/REFERS_TO/GOVERNED_BY edges already produced by cross-document suggestion (`build_legal_cross_document_suggestions`), optionally scoped to one provision (`target_provision`).

For official Department of Lands corpus imports, `app/legal_corpus.py` parses the
five-line source header deterministically. Each instrument stores `legal_work_key`,
`document_class` (`main`, `consolidated`, `amendment`), `version_date`, the official
source URL and corpus reference code. Explicit clauses such as "ให้ยกเลิกความใน
มาตรา ..." become evidence-backed verified AMENDS/REPEALS registry edges. During a
rebuild, an amendment is linked to the latest consolidated expression not newer
than its own version date; this avoids linking a historical act to the current text.

`resolve_instrument_statuses` (`app/legal_registry.py`) is a pure SQL/date resolver — no LLM call — that only touches rows with `status_source = 'resolver'`; a `PATCH /legal-instruments/{id}` admin override sets `status_source = 'manual'` and is never overwritten again. It runs after legal extraction, after a KB-wide legal graph rebuild, and after an admin approves or rejects a suggested relationship (`sync_legal_instrument_relation_review`), so status only changes once a human has verified the relationship that justifies it.

At query time, `app/legal_resolver.py` (`resolve_legal_context`) detects a named instrument or a cited มาตรา/ข้อ provision, selects the version valid at `as_of_date` (default today), expands one hop of AMENDS/ISSUED_UNDER/IMPLEMENTS, and returns a `LegalContext` carrying `current_version_ids` and `excluded_document_ids`. This only runs for a Knowledge Base that actually has legal instruments, so a general KB pays no cost and behaves exactly as before. `query_documents` filters excluded documents out of every channel's evidence (both at the SQL layer and as a channel-agnostic safety net covering LightRAG and graph sources), then `fuse_evidence` boosts reciprocal-rank-fusion scores by authority level, legal status, and recency — a document without a registry entry gets boost 0, so plain retrieval is bit-for-bit unchanged. `validate_legal_evidence` performs a final conflict check: it collapses duplicate provisions across versions to the current one (or flags both under `include_historical`), flags a provision hit by a verified provision-level REPEALS/SUPERSEDES/AMENDS edge, and flags an instrument whose status remains `unknown`. Citations sent to the LLM and returned to the client carry the resolved status, authority level, kind, version label, and effective dates.
