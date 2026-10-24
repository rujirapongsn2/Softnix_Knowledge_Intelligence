# REST API

Base path คือ `/api/v1` และ endpoint ของผู้ดูแลใช้ secure HTTP-only session cookie
ยกเว้น `/api/v1/ingest/...` ที่ใช้ Bearer token สำหรับระบบภายนอก ดู [INGEST_API.md](INGEST_API.md)

## Login และระบบ

- `POST /auth/login`, `POST /auth/logout`, `POST /auth/refresh`, `GET /auth/me`
- `GET /system/status`, `POST /system/test-llm`, `GET /system/graph-projection`
- `GET /traces`, `GET /traces/{trace_id}`, `GET /mcp/activity`
- `POST /system/observability/cleanup`

## Knowledge Base และเอกสาร

- `GET|POST /knowledge-bases`, `DELETE /knowledge-bases/{id}`
- `POST /knowledge-bases/{id}/activate|disable`
- `PATCH /knowledge-bases/{id}/retrieval-config`
- `POST /knowledge-bases/{id}/documents` และ `/documents/batch`
- `GET|POST /knowledge-bases/{id}/document-templates` และ `PATCH|DELETE /document-templates/{id}` — Document Type เป็นฟอร์ม metadata ที่สืบทอด field พื้นฐานจาก Processing Profile และกำหนดความสามารถราย field ได้ (`searchable`, `filterable`, `graph_entity_type`, `graph_relationship`)
- `GET /knowledge-bases/{id}/documents` (legacy list), `GET /knowledge-bases/{id}/documents/page` (bounded UI page with `limit`, `offset`, `search`, `status`, `document_type`, plus global processing/completed/legal flags), `POST /knowledge-bases/{id}/documents/reindex`
- `GET /documents/{id}/text|jobs|file`, `POST /documents/{id}/reprocess`, `DELETE /documents/{id}`, `POST /documents/{id}/restore` — `/file` downloads the stored original (`Content-Disposition: attachment`)
- `PATCH /documents/{id}/metadata` รองรับ `published_at: YYYY-MM-DD` และแก้ค่าฟิลด์ metadata ตาม snapshot ของเอกสาร
- `GET /knowledge-bases/{id}/metadata-export` และ `GET /documents/{id}/metadata-export` ดาวน์โหลดโครงสร้างประเภทเอกสารพร้อมค่า metadata เป็นไฟล์ JSON (`Content-Disposition: attachment`, `schema_version: ski.metadata-export.v1`) ระดับคลังความรู้รวมทุกประเภทเอกสารและทุกเอกสารที่ยังไม่ถูกลบ ระดับเอกสารมีเฉพาะเอกสารนั้นกับประเภทที่ใช้ รูปแบบไฟล์:

  ```json
  {
    "schema_version": "ski.metadata-export.v1",
    "scope": "knowledge_base",
    "exported_at": "2026-10-24T03:00:00+00:00",
    "knowledge_base": {"id": "…", "code": "hr-docs", "name": "เอกสารบุคคล"},
    "document_types": [
      {"id": "…", "code": "circular", "name": "หนังสือเวียน", "description": null, "base_document_type": "general",
       "version": 2, "is_system": false, "is_active": true, "is_current": true,
       "fields": [{"key": "issuer", "label": "ผู้ออก", "field_type": "text", "required": true, "searchable": true, "filterable": false}]}
    ],
    "documents": [
      {"id": "…", "external_id": null, "title": "หนังสือเวียน 12.pdf", "original_filename": "หนังสือเวียน 12.pdf", "status": "completed",
       "document_type": {"id": "…", "version": 2}, "metadata_status": "complete", "metadata_revision": 1,
       "metadata": {"issuer": "กรมทดสอบ"}, "field_states": {}, "updated_at": "2026-10-23T08:15:00"}
    ]
  }
  ```

  เอกสารเก็บ snapshot ของฟิลด์ตาม version ของประเภทที่ใช้ตอนกรอก ประเภทเดียวกันจึงอาจปรากฏหลาย version และ `documents[].document_type` ชี้ไปที่ `id` กับ `version` ใน `document_types` โดย `is_current: false` คือ version เก่าที่ยังมีเอกสารใช้อยู่ `field_states` บอกสถานะการตรวจของฟิลด์ที่ AI สกัด

## Ingestion API (token)

Surface แยกสำหรับระบบภายนอกที่ยืนยันตัวตนด้วย Bearer token ที่มี scope `documents:write` และระบุ
Knowledge Base ไว้ชัดเจน ไม่ใช่ session cookie — รายละเอียด error contract, การ poll สถานะ และตัวอย่าง
curl/Python/Node อยู่ใน [INGEST_API.md](INGEST_API.md)

- `POST /ingest/knowledge-bases/{id}/documents` และ `/documents/batch` (≤20 ไฟล์) — ตอบ `202` เพราะงานเข้าคิว batch รายงานผลรายไฟล์ใน `results[]`
- `GET /ingest/knowledge-bases/{id}/documents` — `status`, `limit` 1-100, `offset`
- `GET /ingest/documents/{id}`, `GET /ingest/documents/{id}/jobs`, และ `GET /ingest/documents/{id}/file` (ดาวน์โหลดต้นฉบับใน KB ของ token)

Knowledge Base และเอกสารที่อยู่นอกสิทธิ์ของ token ตอบ `404` เหมือนไม่มีอยู่จริง เพื่อไม่ให้ไล่เดาทรัพยากรของคนอื่น

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
