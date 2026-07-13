# REST API

Base path: `/api/v1`. Admin APIs use secure HTTP-only login cookies.

- `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`
- `GET|POST /knowledge-bases`, `POST /knowledge-bases/{id}/activate`, `PATCH /knowledge-bases/{id}/retrieval-config`
- `POST /knowledge-bases/{id}/documents` accepts optional `published_at` (`YYYY-MM-DD`), `PATCH /documents/{id}/metadata` updates it, `GET /documents/{id}/text`
- `GET /jobs/{id}`
- `POST /query`, `GET /query/results/{id}/sources`
- `GET|POST /knowledge-bases/{id}/documents`, `GET /documents/{id}/text|jobs`, `POST /documents/{id}/reprocess`
- `GET|POST /knowledge-bases/{id}/entities`, `GET /entities/{id}/sources|graph`
- `GET|POST /knowledge-bases/{id}/relationships`, `POST /query/impact`
- `GET /knowledge-bases/{id}/legal-graph?view=verified|suggested|manual|all`
- `POST|GET /knowledge-bases/{id}/legal-graph/rebuild`, `PATCH /relationships/{id}/legal-review`
- `GET|POST /tokens`, `POST /tokens/{id}/enable|disable|revoke`
- `GET /mcp/activity` returns redacted MCP tool calls with the planner decision and actual retrieval route.
- `GET /traces`, `GET /traces/{trace_id}` return safe RetrievalExecutor root/span traces for the Trace Explorer. Each span contains its channel, status, result count, duration, and relative timing offset; raw prompts, request bodies, tokens, and documents are excluded.

Query responses include `metadata.retrieval_plan` and `metadata.retrieval_trace`. The plan is rule-first; it records graph scope, extracted entity/document identifiers, publication-date bounds, and whether reranking is permitted. OpenRouter is called only for ambiguous queries and its JSON output is constrained by the Knowledge Base policy. A trace marks each channel as `used`, `skipped`, or `unavailable` and includes result count and duration.

`filters` accepts `published_from` and `published_to` (`YYYY-MM-DD`). A month/year stated in a news query populates the same bounds. Documents without `published_at` are excluded when a publication-date filter is active.

Errors have `{status:"error", error:{code,message,retryable}}` and never expose stack traces.
