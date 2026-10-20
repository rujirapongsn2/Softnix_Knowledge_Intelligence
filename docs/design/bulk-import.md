# Bulk Import & Staged Pipeline — Design (draft for approval)

Status: **proposal, not implemented** (rev 2: + §10 manual run-now, §11 resume/checkpoints) · Branch: `feat/extraction-coverage-and-list-fields` · Date: 2026-10-09

Goal: ingest **~50,000 pre-OCR'd text documents** through the API without starving
interactive uploads, make each document **searchable (vector + full-text) as early as
possible**, let expensive stages (LightRAG graph, LLM enrichment) run independently or
off-hours, and give operators an **import status API + UI** with per-stage progress and ETA.

สรุปภาษาไทย: แยกงานประมวลผลเอกสารเป็น "ขั้น" อิสระ (ข้อความ → ค้นหาด้วยเวกเตอร์ได้ → กราฟ
LightRAG → เติม metadata/กฎหมาย) แต่ละขั้นมีคิว/โควตาของตัวเอง เอกสารไหลผ่านทีละขั้น,
มี Import Batch API สำหรับส่งครั้งละ ~100 เอกสาร พร้อม API/หน้าจอดูความคืบหน้า, ETA,
รายการที่ล้มเหลว, retry / pause / cancel และให้งาน interactive ของผู้ใช้แซงคิว bulk เสมอ

---

## 1. Why (evidence from the current system)

| Finding | Evidence |
|---|---|
| One monolithic job does extract → chunk → embed → LightRAG → graph sync | `services.py` `_run_claimed_job` (~L2645–2765) |
| LightRAG dominates time/cost (~3.6 s per 1,200-token chunk; ~11.3k in / 1.9k out tokens per chunk with gleaning + merge summaries) | Oct 8 Acts batch timings, LightRAG LLM cache (5,745 entries) |
| Enrichment follow-ups are slow: metadata ≈ 61 s/doc, legal ≈ 31 s/doc | Oct 8 batch |
| Queue is oldest-first: one 50k import blocks every interactive upload for days | `job_queue.claim_next_job` tiers (follow-up → non-purge → all) |
| Text ingest = 1 doc/request at 60 req/min/token ⇒ ~14 h just to submit 50k | `main.py:3035`, `TokenKey.requests_per_minute=60` |
| No per-KB switch for LightRAG / enrichment | `retrieval.py:120` (`enabled = bool(base_url)`) |
| Retrieval everywhere filters `Document.status == "completed"` | `services.py:737,769,…,3272`, `main.py:1013,1939` |

## 2. Principles

1. **Additive and backward compatible** — existing endpoints, job rows and UI keep working.
2. **Searchable first** — a document is `completed` (= searchable) as soon as its vector stage
   is done; graph and enrichment have their **own status fields** and never block search.
3. **Stage = job type** — each stage is claimed, retried, capped and measured independently.
4. **Interactive beats bulk**, bulk batches share fairly, and every stage has back-pressure.
5. **The database is the source of truth** for progress (no counters that can drift).

---

## 3. Data model

### 3.1 `import_batches` (new)
| Column | Notes |
|---|---|
| `id` uuid, `knowledge_base_id` | one KB per batch (matches ingest token scope) |
| `name`, `source` (e.g. `insightdoc`, `api`, `ui`), `external_ref` | caller labels |
| `created_by_user_id` / `created_by_token_id` | who started it (audit) |
| `status` | `open` → `sealed` → `completed` / `completed_with_errors`; side states `paused`, `cancelling`, `cancelled` |
| `options` JSON | `document_type`, `template_id`, `graph: immediate\|off_hours\|off`, `enrichment: immediate\|off_hours\|off`, `lane: bulk` (default) |
| `idempotency_key` | unique per token; repeated create returns the same batch |
| `items_submitted`, `items_accepted`, `items_duplicate`, `items_rejected` | updated by the submit endpoint (single writer per request) |
| `created_at`, `sealed_at`, `started_at`, `finished_at`, `paused_at`, `cancelled_at` | |

Stage progress is **not** stored as counters on the batch (hot-row contention with many
workers); it is aggregated from documents (§6), cached ~5 s.

### 3.2 `import_items` (new)
`id, batch_id, knowledge_base_id, external_id, checksum_sha256, title, document_id (nullable),
result (accepted|duplicate|rejected), error_code, created_at`.
Unique `(knowledge_base_id, external_id)` where `external_id IS NOT NULL` ⇒ resubmitting an item
is idempotent across requests and batches. Content dedupe keeps using the existing
`uq_document_checksum_live (knowledge_base_id, checksum_sha256)`.

### 3.3 `documents` (new columns, additive)
| Column | Values |
|---|---|
| `import_batch_id` | nullable FK |
| `text_status`, `vector_status`, `graph_status`, `enrichment_status` | `pending, queued, running, done, failed, deferred, skipped` |
| `text_ready_at`, `searchable_at`, `graph_ready_at`, `enriched_at` | per-stage completion timestamps |

`documents.status` stays the user-facing summary with **unchanged meaning for search**:
`queued → extracting → embedding → completed` (searchable) / `failed` / `ocr_required` / `deleted`.
Graph/enrichment progress is shown from the new columns (badges "กราฟ: รอ/กำลังทำ/เสร็จ").
Migration back-fills existing rows: completed docs ⇒ all stage statuses `done` (graph `done`
if `external_engine_id` set, else `skipped`).

### 3.4 `processing_jobs` (new columns)
`lane` (`interactive|bulk|maintenance`, default `interactive`), `import_batch_id`,
`priority` smallint. Index `(status, lane, job_type, next_attempt_at)`.

### 3.5 `document_stage_events` (new, append-only)
`document_id, batch_id, stage, status, started_at, finished_at, duration_ms, error_code, attempt`.
Feeds throughput/ETA and per-stage latency charts; retention 30 days (existing retention task).

### 3.6 KB pipeline settings (in `knowledge_bases.retrieval_config.pipeline`)
```json
{"graph": "immediate", "enrichment": "immediate",
 "off_hours": {"start": "22:00", "end": "06:00", "tz": "Asia/Bangkok"}}
```
Batch `options` may only make a stage *later/off* than the KB setting, never force it on.

---

## 4. Staged pipeline

```
           ┌──────────────┐   ┌──────────────────┐   ┌───────────────┐   ┌──────────────────────┐
 submit ─▶ │ DOC_TEXT     │─▶ │ DOC_VECTOR       │─▶ │ DOC_GRAPH     │   │ DOC_ENRICH_METADATA  │
           │ read/normal- │   │ chunk + embed +  │   │ LightRAG      │   │ DOC_ENRICH_LEGAL     │
           │ ize, OCR if  │   │ metadata values  │   │ ingest + SKI  │   │ (existing jobs)      │
           │ needed       │   │ ⇒ status=completed│  │ graph sync    │   └──────────────────────┘
           └──────────────┘   └────────┬─────────┘   └───────────────┘             ▲
                                       └──────── enqueues (or defers) ─────────────┘
```

| Job type | Work (moved from `_run_claimed_job`) | Typical cost | Default cap (in-flight) |
|---|---|---|---|
| `DOC_TEXT` | `extract_text` (md/txt passthrough, OCR chain for files), store `extracted_text` | ms – minutes (OCR) | 4 (OCR-heavy: 2) |
| `DOC_VECTOR` | `replace_document_chunks`, metadata values, `embed_document_chunks`; set `status=completed`, `searchable_at` | 1–3 s | 6 |
| `DOC_GRAPH` | `index_in_engine` + `sync_lightrag_document_graph` | ~3.6 s/LightRAG chunk | = LightRAG `MAX_PARALLEL_INSERT` (3 → 6–8 in prod) |
| `DOC_ENRICH_METADATA` | existing `EXTRACT_DOCUMENT_METADATA` | ~60 s | 3 |
| `DOC_ENRICH_LEGAL` | existing `EXTRACT_LEGAL_METADATA` | ~30 s | 2 |
| `PURGE_REMOTE_INDEX` | unchanged; runs in the **graph lane budget** (it contends for LightRAG) | | shares DOC_GRAPH cap |

Rules
- Each stage job, on success, **enqueues the next stage in the same transaction** (claim-token
  guarded, as today). DOC_VECTOR enqueues DOC_GRAPH and enrichment jobs, or marks them
  `deferred`/`skipped` according to KB/batch settings.
- Deferred jobs are inserted with `next_attempt_at = next off-hours window start`; a scheduler
  tick re-opens the window each night. Changing the KB setting to `immediate` releases them.
- Enrichment needs only `extracted_text`, so it **does not wait for the graph**; legal graph
  sync keeps the existing KB → document lock order.
- Retrieval already ignores graph-less documents in the LightRAG channel; vector/FTS channels
  see them as soon as `status=completed`.
- Deletion during any stage: same rule as today — queued stage jobs are cancelled, the running
  one finishes its external call then abandons; the purge waits (one running job per document).
- `REPROCESS_DOCUMENT` = DOC_TEXT with `force`; `REINDEX_EMBEDDINGS` = DOC_VECTOR only;
  graph-only re-ingest = DOC_GRAPH (also the **backfill tool** for the LightRAG storage move).
- Compatibility: `PROCESS_DOCUMENT` rows created by old code are executed by a shim that runs
  the stages inline (current behaviour) until drained; new code only creates stage jobs
  (behind `PIPELINE_STAGED=true`).

---

## 5. Scheduling, fairness, back-pressure

### 5.1 Lanes and caps
- `lane=interactive`: UI uploads, single API uploads, reprocess, follow-ups of interactive docs.
- `lane=bulk`: everything created by an import batch.
- `lane=maintenance`: purges, backfills, legal graph rebuilds.
- Per-stage cap = max running jobs of that `job_type` (settings table, editable at runtime).
  A **reserved share** guarantees interactive work: e.g. DOC_GRAPH cap 8, of which bulk may use
  at most 6 ⇒ an interactive upload never waits for a bulk graph job to finish.
- Claim (extends `claim_next_job`, still `FOR UPDATE SKIP LOCKED` + per-document exclusivity +
  claim token):
  1. choose stages whose `running < cap` (counted under a short advisory lock per stage),
  2. within a stage: interactive first, then bulk,
  3. within bulk: **round-robin across active batches** — pick the batch with the fewest
     running jobs in that stage, ties by oldest `last_claimed_at` (stored on the batch);
     paused/cancelling batches are skipped,
  4. then oldest `next_attempt_at/created_at`.
- Workers stay generic (any replica can run any stage); optional `WORKER_STAGES` env lets
  production pin e.g. 2 replicas to `DOC_TEXT,DOC_VECTOR` so cheap stages never queue behind
  long LightRAG polls.

### 5.2 Provider rate limits (OpenRouter)
- Shared **token bucket in Redis** (fallback: PostgreSQL row) per provider bucket:
  `embeddings` (requests/min, tokens/min) and `llm-enrichment` (requests/min, tokens/min).
  `openrouter.py` takes tokens before each call (estimate = `estimate_tokens`), waits up to
  a short budget, else the job is re-queued with `next_attempt_at` (non-blocking).
- On `429/503`: AIMD — halve the bucket rate for 5 min, recover +10 %/min.
- LightRAG's own LLM calls are bounded by LightRAG `MAX_ASYNC`; SKI bounds them indirectly
  through the DOC_GRAPH cap. `OPENROUTER_CREDITS_EXHAUSTED` pauses **all bulk lanes**
  automatically (one alert, no 50k failed jobs).

### 5.3 Ingest back-pressure
- Submit endpoint returns `429 IMPORT_BACKLOG_FULL` + `Retry-After` when the KB's not-yet-
  searchable backlog exceeds a threshold (default 20,000 docs) — senders just retry.
- Item text stored once (Markdown file as today); per request ≤ 100 items and ≤ 25 MB.

---

## 6. Progress, ETA, events

- **Batch progress** = one grouped query over `documents` (`WHERE import_batch_id = :id`,
  indexed) → counts per stage × status, plus `import_items` rejected/duplicate. Cached 5 s.
- **Throughput** per stage = completions in `document_stage_events` over a rolling 15 min
  (and 1 h) window, per batch and global.
- **ETA** per stage = remaining ÷ rolling rate (stage rate is shared with other batches, so use
  this batch's own recent rate; fall back to global rate × fair share). Batch ETA = the latest
  *required* (non-deferred) stage ETA; deferred stages show "starts 22:00".
- **Events**: `document_stage_events` rows; batch status changes recorded in `audit_logs`.
  Optional later: SSE `/api/v1/import-batches/{id}/events` (UI polls every 5 s in v1).

---

## 7. API

Ingest token (machine) paths under `/api/v1/ingest`, admin (session) mirrors under `/api/v1`.
Ingest tokens can read only batches they created (`documents:write` implies own-batch read).

| Method & path | Purpose |
|---|---|
| `POST /ingest/knowledge-bases/{kb}/import-batches` | create batch `{name, source, external_ref, options}`; header `Idempotency-Key` |
| `POST /ingest/import-batches/{id}/items` | submit ≤100 items: JSON `{items:[{external_id, title, text, document_type?, template_id?, metadata?, published_at?}]}` or `application/x-ndjson`. Response per item: `accepted` (document_id) / `duplicate` (existing document_id, by external_id or checksum) / `rejected` (code). Safe to retry. |
| `POST /ingest/import-batches/{id}/seal` | no more items; batch can complete |
| `GET  /ingest/import-batches/{id}` | status, counts per stage/status, throughput, ETA |
| `GET  /ingest/import-batches/{id}/items?result=&stage=&status=failed&cursor=&limit=` | paginated items with document stage statuses + error codes |
| `GET  /ingest/import-batches?status=` | list own batches |
| `POST /import-batches/{id}/retry` `{scope:"failed", stage?:...}` | re-enqueue failed stage jobs (resets attempts) |
| `POST /import-batches/{id}/pause` · `/resume` · `/cancel` `{delete_documents:false}` | pause = claim skips the batch; cancel = cancel queued jobs, running finish, optional soft-delete of batch docs |
| `GET  /system/pipeline` | dashboard: per stage × lane queued/running/deferred/failed, oldest wait, rate/h, provider bucket state, LightRAG health |

Existing endpoints (`/documents`, `/documents/batch`, `/ingest/.../documents`, `/text`,
`/batch`) are unchanged in contract; they create interactive-lane stage jobs. `/text` callers
can switch to batches for 100× fewer requests.

---

## 8. UI

1. **Imports** (new menu under Knowledge/Ingest): table — name, KB, source, created by, status,
   overall progress bar, items (accepted/duplicate/rejected), failed, docs/h, ETA, actions.
2. **Batch detail**: four stage progress bars (ข้อความ · ค้นหาได้ · กราฟ · เติมข้อมูล) with
   queued/running/done/failed/deferred segments; throughput sparkline; ETA; settings summary
   (graph/enrichment immediate/off-hours/off); **failed items table** (title, external_id,
   stage, error code with the existing `documentError.*` Thai/English hints, attempts, last
   error time) with filter, retry selected/all failed, open document; pause/resume/cancel with
   confirm dialogs.
3. **Dashboard widget**: queue depth per stage and lane, oldest waiting age, docs/h,
   "bulk paused (credits exhausted)" banner, LightRAG busy/healthy.
4. **Document view**: stage badges + timestamps; KB settings page gets the pipeline options.
All strings via `translations.js` (en/th parity test already enforced).

---

## 9. Production configuration notes (LightRAG & infra)

- **Storage** (done): LightRAG runs on `PGKVStorage`, `PGVectorStorage`, `PGDocStatusStorage`
  (tables `lightrag_*`, workspace `lightrag`, in the SKI database) and `Neo4JStorage` (label `lightrag`).
  The move and its re-ingest are in `deploy/lightrag-storage/`.
- **Env names** (done): compose sets `MAX_ASYNC` and `MAX_PARALLEL_INSERT`, the names LightRAG 1.5.4 reads,
  and pins the image. Prod starting point: `MAX_ASYNC=16`, `MAX_PARALLEL_INSERT=6`,
  then raise with measured 429 rate.
- Cost/speed levers (A/B on Thai quality first): `MAX_GLEANING=0` (−1 extract call/chunk),
  `FORCE_LLM_SUMMARY_ON_MERGE=16`, cheaper extraction model.
- SKI Postgres: `shared_buffers` 25 % RAM, `maintenance_work_mem` ≥ 1 GB (HNSW), ~15 KB per
  chunk ⇒ ~14 GB for 50k × 10k chars.

---

## 10. Manual "run now" for every scheduled / periodic process

Every process that otherwise runs on a timer or in the off-hours window can be started
manually **now**, from the API and the UI, scoped as narrowly as needed.

| Process | Normal trigger | Manual scopes | Endpoint (admin session) |
|---|---|---|---|
| Deferred graph stage (`DOC_GRAPH`) | off-hours window | global · KB · batch · document(s) | `POST /api/v1/pipeline/run` `{process:"graph", kb_id?, batch_id?, document_ids?, lane?}` |
| Deferred enrichment (`DOC_ENRICH_*`) | off-hours window | global · KB · batch · document(s) · `metadata\|legal` | same, `process:"enrichment"` |
| Stale-job sweep | every 60 s | global | `process:"stale_sweep"` |
| Retention (observability, stage events, items) | hourly | global | `process:"retention"` |
| Remote purge drain (`PURGE_REMOTE_INDEX`) | continuous, serialized | global · KB | `process:"purge_drain"` (clears `next_attempt_at` back-off) |
| Unavailable-document recovery | every 5 min | global · KB | `process:"recovery"` |
| LightRAG backfill (storage move / graph rebuild) | manual only | global · KB · batch | `process:"graph_backfill"` |
| Legal graph rebuild | on demand today | KB | `process:"legal_graph_rebuild"` |
| Provider resume (§11.4) | auto probe | global · batch | `POST /api/v1/pipeline/resume` |

Rules
- **"Run now" never runs work inside the HTTP request.** It (a) releases deferred jobs in scope
  (`next_attempt_at = now`, `deferred → queued`) or (b) enqueues one `MAINTENANCE_RUN` job
  (`lane=maintenance`) for sweep/retention/recovery/backfill. Response: `202 {run_id, released, already_running}`.
- **Singleton guard**: each process × scope takes the PostgreSQL advisory lock already used by
  the worker (`job_queue.try_singleton("stale-job-sweep")` etc., key = `process:scope`). If a run
  is active the API returns `409 PROCESS_ALREADY_RUNNING` with the active `run_id`; a release of
  deferred jobs is idempotent (second call releases 0). Caps and lanes still apply, so "run now"
  cannot overload LightRAG.
- **Permission**: admin (`current_admin`) + KB access (`assert_kb_access`) for scoped runs;
  global processes require system admin. Ingest tokens cannot trigger maintenance.
- **Audit**: `record_audit("pipeline.run", user, process, scope, released_count, run_id)`; each
  run stored in `maintenance_runs (id, process, scope, requested_by, status, started_at,
  finished_at, result JSON, error)` and shown in the UI history.
- `GET /api/v1/pipeline/runs?process=&status=` lists runs; `GET /api/v1/pipeline/schedule` shows
  each process's next automatic run and last result.

UI
- **Dashboard → Pipeline panel**: one row per process: last run, next run, status, **Run now**
  button (disabled + spinner while running, shows 409 owner), history drawer.
- **Batch detail**: buttons "Run graph now", "Run enrichment now" (only when deferred),
  "Retry failed", "Resume".
- **KB settings**: "Run deferred graph/enrichment for this KB now", "Rebuild graph (backfill)".
- **Document view**: "Run this stage now" per deferred/failed stage.
- Confirm dialog shows the count that will be released and the estimated cost (§11.6).

## 11. Resume without redoing work (checkpoints & idempotency)

Any stopped or failed stage — OpenRouter 402 credits, 401 auth, 429 rate limit, 5xx outage,
LightRAG down/busy/timeout, worker crash — can be resumed after the cause is fixed and
**continues from its last checkpoint**. Nothing already done is paid for or written twice.

### 11.1 Stage outputs are idempotent (key = document + stage + content hash)
- `documents.text_sha256` = hash of normalized `extracted_text`. Every stage records the
  `input_hash` it consumed in `document_stage_state (document_id, stage, input_hash, status,
  checkpoint JSON, attempts, last_error_code, cost_tokens_in/out, saved_tokens, updated_at)`,
  unique `(document_id, stage)`.
- A stage whose `status=done` and `input_hash` equals the current input is **skipped** on retry,
  reprocess, resume or backfill. Changed text ⇒ new hash ⇒ the stage (and only its successors) re-runs.
- Writes are upserts on natural keys, never blind inserts:
  - chunks: unique `(document_id, chunk_index)` + `content_sha256`; a chunk whose hash is
    unchanged keeps its row **and its embedding**;
  - vectors: written only where `embedding IS NULL` or the chunk hash changed;
  - LightRAG: one remote document per SKI document, id/file_path derived from
    `document_id` (existing `softnix-kb=…__doc=…` convention) — re-ingest first checks
    `find_document` and re-uses it;
  - SKI graph rows: upsert by `(knowledge_base_id, normalized name)` / relationship key
    + `entity_sources/relationship_sources` unique per `(entity, document, chunk)`;
  - metadata values: upsert per `(document_id, field_key)`;
  - import items: unique `(knowledge_base_id, external_id)`; documents: existing
    `uq_document_checksum_live`.

### 11.2 Sub-step checkpoints per stage
| Stage | Checkpoint (in `document_stage_state.checkpoint`) | Resume behaviour |
|---|---|---|
| `DOC_TEXT` | `text_sha256`, OCR pages done (`ocr_pages_done:[…]`) | skip if text present with same source checksum; OCR resumes at first missing page |
| `DOC_VECTOR` | `chunks_hash` (hash of chunk list), `embedded_chunk_ids` or ranges `[[0,127],[128,255]]` per embedding batch | re-chunk only if `text_sha256` changed; embed only chunks with `embedding IS NULL` — each committed batch is a checkpoint |
| `DOC_GRAPH` | `lightrag_doc_id`, `track_id`, remote `status` (`pending/processing/processed/failed`), `remote_content_hash`, `graph_synced_labels` | if remote doc `processed` with same hash ⇒ skip ingest, only (re)run SKI graph sync; if `processing` ⇒ poll the same `track_id`; if `failed` ⇒ re-insert — LightRAG's **LLM response cache** (`ENABLE_LLM_CACHE` + `ENABLE_LLM_CACHE_FOR_EXTRACT`, keyed by chunk content) returns already-extracted chunks without new LLM calls |
| `DOC_ENRICH_METADATA` | `windows_done:{index: result}`, `template_version`, `text_sha256` | re-run only missing windows; publish when all windows done (existing CAS on `metadata_revision`) |
| `DOC_ENRICH_LEGAL` | deterministic parse result, `llm_done` | skip LLM if deterministic parse sufficed or result already stored |

Checkpoints are written in the same transaction as the output they describe (claim-token
guarded), so a crash can never record progress that was not saved.

### 11.3 Failure classes
| Error | Class | Action |
|---|---|---|
| `OPENROUTER_CREDITS_EXHAUSTED` (402), `OPENROUTER_AUTH_FAILED` (401) | **provider-blocking** | job → `paused_by_provider` (not failed, attempts not consumed); provider breaker opens (§11.4) |
| 429 / `OPENROUTER_UNAVAILABLE` / 5xx / timeout | transient | bucket back-off (§5.2) + `PROVIDER_RETRY`; breaker opens after N consecutive failures in a window |
| `RETRIEVAL_ENGINE_UNAVAILABLE` / `_TIMEOUT` / `_BUSY` | engine-blocking | graph lane `paused_by_engine`; other stages continue (vector search keeps working) |
| validation / content errors (`TEXT_EXTRACTION_EMPTY`, `REQUEST_REJECTED`, too large) | permanent | stage `failed`; fix data then "Retry failed" |

### 11.4 `paused_by_provider` and Resume
- A **provider breaker** row per provider (`openrouter-llm`, `openrouter-embeddings`,
  `lightrag`): `closed | open | half_open`, `reason`, `opened_at`, `opened_by_job`.
- On 401/402 (or breaker trip): breaker opens, **all bulk-lane jobs needing that provider are
  not claimed** (claim filter), running ones finish their current sub-step and park as
  `paused_by_provider` with their checkpoint; batches show status `paused_by_provider` with the
  reason and a Thai/English hint ("เติมเครดิต OpenRouter แล้วกด Resume"). Interactive jobs
  also park (no point failing them) but are released first on resume.
- **Resume** (`POST /api/v1/pipeline/resume {scope: global|batch, batch_id?, provider?}`, UI
  button on dashboard banner and batch page): runs a cheap **probe call** (1 embedding / 1-token
  LLM); success ⇒ breaker `closed`, parked jobs `queued` with `next_attempt_at` spread over
  ~1 min (no thundering herd); failure ⇒ 409 with the provider error, nothing released.
- Auto-probe every 10 min while open (half-open), so a credit top-up resumes without a click;
  manual Resume is always available. Singleton + audit as in §10.

### 11.5 Retry failed (only failed stages)
`POST /import-batches/{id}/retry {scope:"failed", stage?, error_code?}` and per-document
"Retry stage": re-enqueues **only** stages with `status=failed` (successors stay pending),
resets their attempt counter, keeps checkpoints, and never touches `done` stages. Pause/cancel
state is respected; a retry on a cancelled batch is refused (409).

### 11.6 Cost / time accounting
- Every provider call records `tokens_in/out`, model and duration on `document_stage_state`
  (and LightRAG reports via doc status chunk counts × measured per-chunk tokens; exact when
  LightRAG usage hooks are available).
- On resume/retry, skipped chunks/windows/stages add to `saved_tokens` and `saved_seconds`
  (estimated from the stage's rolling average) — LightRAG cache hits counted from its cache stats.
- Batch detail and the run-now/resume dialogs show: tokens & estimated USD spent, **work saved
  by resume** (chunks not re-embedded, windows not re-read, chunks served from LightRAG cache),
  and the estimated cost of what remains.

### 11.7 Guarantees (invariants checked by tests and a nightly consistency job)
- one live document per `(kb, checksum)` and per `(kb, external_id)`;
- chunks unique per `(document_id, chunk_index)`; no chunk without embedding once `vector=done`;
- at most one LightRAG document per SKI document (`find_document` count = 1), none for deleted;
- SKI entity/relationship sources unique per `(entity|relationship, document, chunk)`;
- a `done` stage is never re-executed for the same `input_hash`.
`GET /api/v1/pipeline/consistency?kb_id=` (admin, run-now capable) reports violations.

## 12. Migration & rollout

| Phase | Content | Flag / safety |
|---|---|---|
| 0 | LightRAG env-name fix; staging with PG/Neo4j storage + backfill test | config only |
| 1 | Alembic: `import_batches`, `import_items`, `document_stage_events`, new columns + back-fill | additive, no behaviour change |
| 2 | Stage job types, shim for `PROCESS_DOCUMENT`, lanes, caps, fair claim, token bucket; `document_stage_state` checkpoints + idempotent upserts (§11.1–11.2); provider breaker + `paused_by_provider` (§11.3–11.4) | `PIPELINE_STAGED` (off ⇒ today's path) |
| 2b | Manual run-now API + `maintenance_runs` + singleton/audit (§10); resume/retry-failed endpoints; cost accounting (§11.6) | admin-only endpoints |
| 3 | Import batch API + status API + docs (`docs/INGEST_API.md`) | new endpoints only |
| 4 | UI pages + dashboard widget + Pipeline panel (Run now, Resume, run history, saved-work display) | — |
| 5 | Pilot: 500 → 5,000 → 50,000 docs on production; watch rate, 429s, cost per 1k docs; **drill**: revoke key / exhaust credits mid-batch, fix, Resume, verify no rework | pause-all switch, credits auto-pause |

Rollback: turn `PIPELINE_STAGED` off — the shim keeps processing; new tables stay unused.

## 13. Test plan

- **Unit**: stage state machine & chain enqueue, defer/skip per KB+batch, idempotent submit
  (external_id + checksum), pause/resume/cancel/retry semantics, ETA math, token bucket & AIMD.
- **PostgreSQL concurrency** (throwaway `*_test` DB): caps never exceeded with N workers;
  interactive job claimed ahead of a 10k bulk backlog; two batches progress ~equally;
  per-document exclusivity and claim-token CAS still hold across stages; delete mid-stage.
- **Integration** (fake LightRAG/OpenRouter): 429 → backoff, credits-exhausted → bulk paused,
  LightRAG BUSY → graph lane backs off, deferred window release.
- **Migration**: upgrade on a copy of production schema; legacy `PROCESS_DOCUMENT` rows drain.
- **E2E (temp KB)**: 200-doc batch via JSONL; searchable before graph; pause/resume; cancel;
  retry failed; one interactive upload during the batch finishes within its normal time.
- **Load (staging)**: 5k docs, graph off-hours; measure docs/h per stage and cost per 1k docs.
- **Web**: progress/ETA rendering, failed-items table, en/th translation parity; Run-now / Resume buttons disabled while running and showing 409 owner.
- **Manual run-now (§10)**: every process callable via API for each scope; second concurrent call ⇒ 409
  `PROCESS_ALREADY_RUNNING` (PostgreSQL advisory lock, two sessions); release of deferred jobs is
  idempotent; non-admin / wrong-KB ⇒ 403; audit row + `maintenance_runs` row written; caps still
  respected after a global release (never more than cap running).
- **Resume / checkpoints (§11)**: kill a worker or inject 402 after embedding batch k ⇒ resume
  embeds only batches > k (provider call count asserted); LightRAG doc `processed` ⇒ resume skips
  ingest and only syncs graph; `processing` ⇒ re-polls the same track id; metadata windows done
  are not re-requested; 401/402 ⇒ jobs `paused_by_provider`, attempts unchanged, bulk not
  claimed, interactive parked; Resume with failing probe ⇒ 409 and nothing released; Resume OK ⇒
  all parked jobs continue; "retry failed" re-runs only failed stages; rerun of a `done` stage with
  same hash is a no-op; duplicate submit/retry produces no extra documents, chunks, vectors,
  LightRAG docs or graph sources (invariant queries §11.7); saved-work counters > 0 after resume.
- **E2E drill (temp KB, 200 docs)**: set an invalid OpenRouter key mid-batch ⇒ batch shows
  `paused_by_provider` + hint; restore key; click Resume ⇒ batch completes; provider-call count ≈
  one pass (≤ 5 % overhead); consistency endpoint reports 0 violations.

### Acceptance criteria
1. Each process in §10 has a working API endpoint and UI button for every listed scope, runs
   at most once concurrently (409 otherwise), requires admin/KB permission, and is audited.
2. After any provider/engine failure in §11.3 and the fix, Resume completes the batch with
   **no re-embedded unchanged chunk, no re-read finished metadata window, no second LightRAG
   ingest of a processed document**, and **0 invariant violations** (§11.7).
3. 401/402 never turns bulk documents into `failed`; they become `paused_by_provider` within one
   claim cycle and the dashboard shows the cause and the Resume action.
4. "Retry failed" changes only stages that were `failed`.
5. Batch detail shows spent cost and saved work after a resume.

## 14. Estimates (for planning; assumes avg 10k chars/doc, gpt-4o-mini)

| Scenario | Searchable (vector) | Graph complete | LLM cost |
|---|---|---|---|
| Current single pipeline | ~7 days (blocked by JSON storage) | same | ~$700–1,000 |
| Staged + prod config (MAX_ASYNC 16, 6 parallel inserts) | **~6–10 h** | +28–42 h (can run off-hours) | ~$450–1,000 (gleaning off/on) |

Effort (rough): Phase 1–2 ≈ 4–6 dev-days, checkpoints/resume/breaker ≈ 3–4, run-now/maintenance runs ≈ 2, Phase 3 ≈ 2–3, Phase 4 ≈ 4–5, tests/rollout/drill ≈ 4.

## 15. Open questions for approval

1. `status=completed` means "searchable" (graph may still run) — OK, or a new `searchable` status?
2. Default per-KB: graph `immediate` for interactive, `off_hours` for bulk?
3. Cancel default: keep already-created documents (proposed) or soft-delete them?
4. Ingest tokens may read their own batches' status (proposed) — or admin-only?
5. Off-hours window (proposed 22:00–06:00 Asia/Bangkok) and bulk share of each stage cap (75 %)?
6. Batch/item retention (proposed: items 90 days, stage events 30 days)?
7. Global run-now (sweep, retention, global graph release) — system admin only (proposed) or any admin?
8. On 401/402 also park **interactive** jobs (proposed) or let them fail fast as today?
9. Auto-probe/auto-resume every 10 min when the provider breaker is open (proposed) or manual Resume only?
10. Keep LightRAG LLM cache permanently enabled (proposed; needed for cheap resume) — accept its storage growth?
