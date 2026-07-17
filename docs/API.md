# REST API

Base path: `/api/v1`. Admin APIs use secure HTTP-only login cookies.

- `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`
- `GET|POST /knowledge-bases`, `POST /knowledge-bases/{id}/activate`, `PATCH /knowledge-bases/{id}/retrieval-config`
- `POST /knowledge-bases` accepts `name` and an optional `code`; when omitted, the API derives a collision-safe code (including for Thai/non-Latin names) and adds a suffix when a prior or soft-deleted code exists.
- `POST /knowledge-bases/{id}/documents` accepts optional `published_at` (`YYYY-MM-DD`), `PATCH /documents/{id}/metadata` updates it, `GET /documents/{id}/text`
- `GET /jobs/{id}`
- `POST /query`, `GET /query/results/{id}/sources`
- `GET|POST /knowledge-bases/{id}/documents`, `GET /documents/{id}/text|jobs`, `POST /documents/{id}/reprocess`
- `GET|POST /knowledge-bases/{id}/entities`, `GET /entities/{id}/sources|graph`
- `GET|POST /knowledge-bases/{id}/relationships`, `POST /query/impact`
- `GET /knowledge-bases/{id}/legal-graph?view=verified|suggested|manual|all`
- `POST|GET /knowledge-bases/{id}/legal-graph/rebuild`, `PATCH /relationships/{id}/legal-review`
- `GET /knowledge-bases/{id}/legal-registry?status=&kind=&family_id=&document_id=` lists legal instruments with kind, authority level, status, and effective dates
- `GET /knowledge-bases/{id}/legal-registry/worklist` lists missing provenance/effective dates, unreviewed instruments, unresolved relation targets, and missing evidence for curator review
- `GET /legal-instruments/{id}` returns the instrument, its family/version chain, and incoming/outgoing cross-instrument relations
- `PATCH /legal-instruments/{id}` accepts an admin override plus `source_uri` and `source_reference`; it marks the instrument as manually verified and always wins over the automatic resolver
- `POST /knowledge-bases/{id}/legal-registry/resolve` re-runs the deterministic in_force/amended/superseded/repealed status resolver
- Legal registry responses also expose `legal_work_key`, `document_class`, and `version_date`; official corpus source URL/reference and amendment evidence are retained in provenance.
- `GET|POST /tokens`, `POST /tokens/{id}/enable|disable|revoke`
- `GET /mcp/activity` returns redacted MCP tool calls with the planner decision and actual retrieval route.
- `GET /traces`, `GET /traces/{trace_id}` return safe RetrievalExecutor root/span traces for the Trace Explorer. The trace includes a bounded request preview, query length/hash, filter summary, answer preview, citation IDs, planner rationale/policy version, and per-span input/output summaries. Each span contains its channel, status, result count, reason code, duration, and relative timing offset. Tokens, headers, full prompts, provider payloads, and document bodies are excluded.
- Trace list APIs support `cursor`, `from_ts`, `to_ts`, `transport`, `status`, `tool`, and `paginate=true`. The paginated response is `{items,next_cursor,has_more,limit}`; the default response remains a list for backwards compatibility.
- `POST /system/observability/cleanup` runs bounded retention for trace/request/MCP observability records. Automatic worker cleanup uses `OBSERVABILITY_RETENTION_DAYS` (default 30) and `AUDIT_RETENTION_DAYS` (default 180).

Query responses include `metadata.retrieval_plan` and `metadata.retrieval_trace`. The plan is rule-first; it records graph scope, extracted entity/document identifiers, publication-date bounds, and whether reranking is permitted. OpenRouter is called only for ambiguous queries and its JSON output is constrained by the Knowledge Base policy. A trace marks each channel as `used`, `skipped`, or `unavailable` and includes result count and duration.

`filters` accepts `published_from` and `published_to` (`YYYY-MM-DD`). A month/year stated in a news query populates the same bounds. Documents without `published_at` are excluded when a publication-date filter is active.

`filters` also accepts `as_of_date` (`YYYY-MM-DD`, defaults to today) and `include_historical` (defaults to `false`) for Knowledge Bases with a legal registry. The planner resolves which instrument version was in force at that date, excludes documents it knows are repealed or superseded, and boosts fused ranking by authority level, legal status, and recency; `include_historical: true` disables the exclusion and keeps every version for comparison. A KB with no legal instruments is unaffected by these fields. Query responses may include `warnings` (e.g. `SUPERSEDED_VERSION_REMOVED`, `PROVISION_REPEALED`, `PROVISION_AMENDED`, `UNVERIFIED_VALIDITY`) and each source may carry `document_status`, `authority_level`, `kind`, `version_label`, `effective_from`, `effective_to`, and a combined `legal_label` used in the citation header sent to the LLM.

Errors have `{status:"error", error:{code,message,retryable}}` and never expose stack traces.
