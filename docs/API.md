# REST API

Base path คือ `/api/v1` และ endpoint ของผู้ดูแลใช้ secure HTTP-only session cookie

## Login และระบบ

- `POST /auth/login`, `POST /auth/logout`, `POST /auth/refresh`, `GET /auth/me`
- `GET /system/status`, `POST /system/test-openrouter`, `GET /system/graph-projection`
- `GET /traces`, `GET /traces/{trace_id}`, `GET /mcp/activity`
- `POST /system/observability/cleanup`

## Knowledge Base และเอกสาร

- `GET|POST /knowledge-bases`, `DELETE /knowledge-bases/{id}`
- `POST /knowledge-bases/{id}/activate|disable`
- `PATCH /knowledge-bases/{id}/retrieval-config`
- `POST /knowledge-bases/{id}/documents` และ `/documents/batch`
- `GET|POST /knowledge-bases/{id}/document-templates` และ `PATCH|DELETE /document-templates/{id}` — Document Type เป็นฟอร์ม metadata ที่สืบทอด field พื้นฐานจาก Processing Profile และกำหนดความสามารถราย field ได้ (`searchable`, `filterable`, `graph_entity_type`, `graph_relationship`)
- `GET /knowledge-bases/{id}/documents` (legacy list), `GET /knowledge-bases/{id}/documents/page` (bounded UI page with `limit`, `offset`, `search`, `status`, `document_type`, plus global processing/completed/legal flags), `POST /knowledge-bases/{id}/documents/reindex`
- `GET /documents/{id}/text|jobs`, `POST /documents/{id}/reprocess`, `DELETE /documents/{id}`, `POST /documents/{id}/restore`
- `PATCH /documents/{id}/metadata` รองรับ `published_at: YYYY-MM-DD` และแก้ค่าฟิลด์ metadata ตาม snapshot ของเอกสาร

## Query และ graph

- `POST /query` — query ปกติ; ผลมี `metadata.retrieval_plan` และ `metadata.retrieval_trace`
- `GET /query/results/{id}/sources`, `POST /query/results/{id}/feedback`
- `POST /query/impact`
- `GET|POST /knowledge-bases/{id}/entities`, `PATCH|DELETE /entities/{id}`
- `GET /entities/{id}/sources|graph|inspector`
- `GET|POST /knowledge-bases/{id}/relationships`, `PATCH|DELETE /relationships/{id}`

## Legal registry

- `POST /documents/{id}/legal-extract`
- `PUT|PATCH|DELETE /documents/{id}/legal-metadata`
- `GET /knowledge-bases/{id}/legal-graph|legal-map`
- `POST|GET /knowledge-bases/{id}/legal-graph/rebuild`
- `GET /knowledge-bases/{id}/legal-registry`, `/legal-registry/worklist`
- `GET|PATCH /legal-instruments/{id}`
- `POST /knowledge-bases/{id}/legal-registry/resolve`

## Filters และ retrieval trace

`QueryRequest.filters` รองรับ `published_from`, `published_to`, `as_of_date`, `include_historical` และ `metadata` แบบ key/value เฉพาะฟิลด์ที่ประกาศ `filterable=true` เอกสารที่ไม่มี `published_at` จะไม่ถูกคืนเมื่อใช้ publication date filter ฟิลด์ที่ประกาศ `searchable=false` จะไม่ถูกนำไปค้นแบบ full-text และฟิลด์ที่ map graph เท่านั้นจึงจะสร้าง node/edge ที่มี provenance ลง graph

Plan ระบุ intent, channels, graph scope/depth, entity/document identifiers, date range และสิทธิ์ rerank ส่วน trace ระบุ channel ว่า `used`, `skipped` หรือ `unavailable`, จำนวนผล, เหตุผล และเวลา โดยไม่เปิดเผย token, header, prompt เต็ม หรือเนื้อหาเอกสาร

ข้อผิดพลาดใช้รูปแบบ `{status:"error", error:{code,message,retryable}}` และไม่ส่ง stack trace
