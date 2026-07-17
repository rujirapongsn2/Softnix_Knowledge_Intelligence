# High-level Data Flow Diagram

This diagram describes the production MVP flow from document ingestion to a
cited answer returned through the REST API or Remote MCP. PostgreSQL is the
canonical source for evidence and provenance; Neo4j is a bounded graph
accelerator and projection, not the citation authority.

```mermaid
flowchart LR
  Admin[Administrator / browser]
  Agent[MCP client / AI agent]
  Web[Admin Web UI]
  API["FastAPI API<br/>REST + MCP"]
  Auth["Authentication and authorization<br/>session or scoped MCP token"]
  Queue[("Redis<br/>queue / concurrency")]
  Worker[Async document worker]
  Files[("Local file storage<br/>original documents")]
  PG[("PostgreSQL<br/>metadata, chunks, pgvector, FTS,<br/>legal registry, provenance, audit")]
  Extract["MarkItDown extraction<br/>Markdown normalization and chunking"]
  OCR["External OCR<br/>scanned PDF only"]
  OpenRouter["OpenRouter<br/>embeddings, planner fallback,<br/>answer and optional reranker"]
  Neo4j[("Neo4j<br/>graph projection / bounded traversal")]
  LightRAG[("LightRAG<br/>semantic graph retrieval adapter")]
  Planner[Retrieval Planner]
  Executor[Parallel Retrieval Executor]
  Fusion["Fusion, rerank and<br/>legal evidence validation"]
  Citation["Citation service<br/>provenance and source mapping"]
  Trace[("Trace spans, MCP activity<br/>and audit records")]

  Admin -->|HTTPS| Web
  Web -->|login, KB, upload,<br/>query and graph actions| API
  API --> Auth
  Auth -->|authorized admin request| API

  API -->|validate MIME, size,<br/>checksum and duplicate| API
  API -->|save original| Files
  API -->|document metadata and job| PG
  API -->|enqueue| Queue
  Queue --> Worker
  Worker -->|read local file| Files
  Worker --> Extract
  Extract -->|no usable PDF text| OCR
  OCR -->|Markdown result| Extract
  Extract -->|normalized Markdown,<br/>chunks and provenance| PG
  Worker -->|embeddings / legal extraction| OpenRouter
  OpenRouter -->|vectors and structured metadata| PG
  Worker -->|graph projection event| Neo4j
  Worker -->|document index sync| LightRAG
  Worker -->|job status, errors and retry| PG

  Agent -->|HTTPS /mcp + Bearer token| API
  API --> Auth
  Auth -->|token scope, expiry,<br/>tool, rate and concurrency limits| Planner
  Planner --> Executor
  Executor -->|vector + full-text| PG
  Executor -->|bounded graph IDs| Neo4j
  Neo4j -->|IDs only| PG
  Executor -->|semantic / graph evidence| LightRAG
  Executor -->|fallback when Neo4j is unavailable<br/>or evidence is incomplete| PG
  PG --> Fusion
  LightRAG --> Fusion
  Fusion --> Citation
  Citation -->|scope-filtered cited evidence| OpenRouter
  OpenRouter -->|answer draft| Citation
  Citation -->|summary, structured result,<br/>citations and request ID| API
  API -->|MCP or REST response| Agent

  API -.->|request, planner and channel spans| Trace
  Worker -.->|processing and projection spans| Trace
  Trace -->|redacted observability data| PG
  Web -->|trace explorer and activity| API
```

## Flow summary

1. **Ingestion** — the API authenticates the administrator, validates the
   upload, stores the original file, and creates a durable processing job.
2. **Extraction** — the worker converts the local file to canonical Markdown
   with MarkItDown. Scanned PDFs can use the configured external OCR service;
   otherwise they remain `OCR_REQUIRED`.
3. **Indexing** — PostgreSQL receives chunks, embeddings, full-text data,
   legal metadata, and provenance. Neo4j and LightRAG receive projections or
   adapter-specific indexes asynchronously.
4. **Authorization** — every query is checked against the session or MCP token.
   MCP token scope is authoritative; a client cannot broaden its Knowledge Base
   scope by sending extra IDs.
5. **Retrieval** — the planner selects permitted channels. Vector, FTS, graph,
   and LightRAG channels can execute in parallel. Neo4j returns bounded IDs;
   PostgreSQL resolves the evidence and citations. PostgreSQL traversal is the
   fallback when the projection is unavailable or stale.
6. **Answering** — results are fused, optionally reranked, filtered for legal
   version/status rules, and mapped to source citations before an answer is
   generated.
7. **Observability** — API and worker spans record the request, plan, actual
   route, fallbacks, durations, result counts, and errors without storing token
   secrets or authorization headers.

## Data ownership boundaries

| Component | Responsibility | Authority |
|---|---|---|
| File storage | Original uploaded files | Source artifact |
| PostgreSQL | Metadata, chunks, vectors, FTS, legal registry, provenance, audit | Canonical system of record |
| Neo4j | Projected entities/relationships and bounded traversal | Retrieval accelerator |
| LightRAG | Adapter-managed semantic/graph retrieval index | Retrieval supplement |
| OpenRouter | Embedding, constrained planner fallback, rerank/answer generation | External processing dependency |
| Redis | Queue and runtime coordination | Ephemeral/durable job coordination |
