# Automatic metadata: implementation and rollout

This release implements the upload/extraction/review workflow and extends MCP retrieval. Document type is still chosen by the uploader; automatic mixed-type classification and bulk template generation remain later phases of the design.

## วิธีใช้งานกระบวนการ

1. เปิด **จัดการประเภทเอกสาร (Manage document types)** ตั้งค่า **วิธีเติมข้อมูล (Fill method)** ของ field เป็น **อ่านจากเอกสาร (Read from document)** และระบุ **ข้อมูลที่ต้องการให้ระบบอ่าน (What to extract)** เมื่อชื่อ field เพียงอย่างเดียวอาจตีความได้ไม่ชัดเจน field ที่เพิ่มใหม่ใน Web Editor จะตั้งค่าให้อ่านจากเอกสารเป็นค่าเริ่มต้น ส่วน template เดิมจะคงพฤติกรรมเดิมจนกว่าจะมีการแก้ไข
2. อัปโหลดไฟล์ผ่านแบบฟอร์มหน้าเดียว field ที่ต้องสกัดจะไม่ขัดขวางการอัปโหลดอีกต่อไป field ที่ผู้ใช้ต้องกรอกเองยังคงแสดงอยู่ ส่วน field ที่ไม่บังคับจะอยู่ใน **ข้อมูลเพิ่มเติม (Additional details)** เมื่ออัปโหลดหลายไฟล์ ระบบจะแสดงเฉพาะ field ที่เปิดใช้ค่าเดียวกันทั้งชุดอย่างชัดเจนเท่านั้น
3. หลังจัดทำดัชนีเนื้อหาเสร็จ งาน `EXTRACT_DOCUMENT_METADATA` จะแยกทำงานเพื่อเติม field ที่ยังขาด หากงานนี้ล้มเหลว สถานะที่เอกสารค้นหาได้จะไม่เปลี่ยนแปลง ระบบใช้การตั้งค่าและโมเดล OpenRouter ที่มีอยู่
4. กรอง Library ด้วยสถานะ **ข้อมูลรอตรวจ (Metadata needs review)** เปิดเอกสารเพื่อตรวจค่าที่ระบบเสนอเทียบกับข้อความหลักฐาน แก้ไขค่าเฉพาะ field หรือบันทึกว่าไม่พบ field นั้นในเอกสารได้ สำหรับเอกสารที่มี field เดิมหรือกรอกด้วยตนเอง ให้กด **เติมข้อมูลที่ขาดอัตโนมัติ (Auto fill missing fields)** เพื่อเปิดการสกัดเฉพาะค่าที่ว่างของเอกสารนั้น โดยไม่แก้ไข template
5. field ที่ยืนยันหรือแก้ไขแล้วจะถูกล็อกไม่ให้การสกัดครั้งถัดไปเขียนทับ มุมมองข้อมูลทั้งหมดจะยังแสดงค่าที่ระบบรับอัตโนมัติเพื่อให้ตรวจสอบและแก้ไขได้

## Trust and limits

- Candidate quotes must occur verbatim in the extracted text. Each candidate carries character offsets, a SHA-256 of the text version, and extractor/template versions; no page number is invented.
- Only a single literal text/select value with valid evidence and no conflict can be auto-accepted. Dates, numbers, booleans, graph-mapped fields, and fields marked **Always require review** need confirmation. Automatic acceptance is a conservative heuristic, not a calibrated accuracy score.
- Candidate values remain separate from effective metadata. Graph projection accepts human-verified or legacy manual values; AI candidates are never labeled verified.
- The first release processes up to four 12,000-character windows per extraction. Longer documents are explicitly marked partial and no values are auto-accepted from that partial pass. Missing values and conflicts remain reviewable. Extraction retries provider availability errors up to three attempts.
- Values already entered by users remain effective. Reprocessing changed text invalidates unlocked automatic values and their exact-filter projection. Publication checks document revision, source text and template version to discard results made stale by concurrent editing.
- Required extraction fields mean required for completion of metadata review; a user may explicitly record “not found.” They do not become an upload or search-availability gate.
- This release does not automatically interpret relative legal commencement dates, normalize organization aliases, classify document types, run bulk backfills, or claim measured retrieval/accuracy improvements. Legal registry review remains authoritative for legal status.

## API additions

- `POST /api/v1/documents/{id}/metadata-extract`: `{ "enable_missing_fields": false }`. Set true to explicitly enable extraction for blank fields in an existing document. Returns whether a job was queued; an active job is not duplicated.
- `POST /api/v1/documents/{id}/metadata-review`: `{ "revision": 1, "field_key": "issuer", "action": "confirm", "candidate_index": 0 }`. Other actions are `set` with a typed `value`, and `not_found`. A stale revision returns HTTP 409.
- The text preview and document list include `metadata_status`, `metadata_revision`, and `metadata_observations`.
- Document page filtering accepts `metadata_status=needs_review`, including metadata extraction failures.
- Existing metadata PATCH clients remain supported. Saving through that endpoint records the explicitly supplied document metadata as manual and locks it against extraction.

## MCP

`describe_knowledge_schema` is a new read-only, explicitly granted tool. Existing tokens are not given new permissions. It returns current templates plus historical snapshots from a bounded document page, typed operators and page-scoped coverage. Follow `next_offset`; coverage is not a whole-KB percentage until all pages have been accounted for. No document text is loaded for schema discovery.

`search_knowledge` and `document_inventory_summary` accept the following explicit filter form alongside existing `filters.metadata` equality filters:

```json
{
  "filters": {
    "metadata_predicates": [
      {
        "template_id": "<template ID returned by schema discovery>",
        "field_key": "effective_date",
        "field_type": "date",
        "operator": "gte",
        "values": ["2026-01-01"]
      },
      {
        "template_id": "<same template ID>",
        "field_key": "effective_date",
        "field_type": "date",
        "operator": "lte",
        "values": ["2026-12-31"]
      }
    ]
  }
}
```

Predicates are ANDed, require a filterable field and matching type, and use indexed date/number columns. `eq` and `in` work for all supported field types; `gte` and `lte` are date/number only. String equality is exact and case-sensitive. Template identity prevents unrelated custom fields with the same key from matching each other. The legacy key-only filter remains compatible.

Unknown values do not satisfy explicit filters, and filters are never relaxed silently. Results include status and per-predicate coverage for authorized non-deleted documents; this scope is broader than a particular query's temporal/searchable subset. These counts must not be described as complete legal/current-document counts.

Search sources include effective searchable metadata, trust, source evidence and revision. The inline projection prioritizes requested keys and is limited to eight fields with 1,000-character values/quotes, with truncation markers. Saved `get_sources` results retain that versioned evidence. Non-searchable metadata is not attached to answer context. Inferred types are not added as hard filters.

## Database rollout

Migration `0031_auto_metadata` adds document observation/status/revision columns and typed filter projections, then backfills typed values for existing indexed fields. It handles fresh installations whose initial migration creates tables from current models. Run the migration before starting the updated API/worker; deploy the matching web build too.

For this repository's Compose setup:

```sh
docker compose build api worker migrate web
docker compose run --rm migrate
docker compose up -d api worker web
```

The changes were developed against an isolated test database, not applied to the production database. No existing templates are silently migrated to extraction mode. No actual provider credentials are needed for the automated tests; the extraction provider is mocked.

## Verification

Regression coverage includes literal evidence verification, invalid quotes, conflicting and partial extraction, typed values including boolean false, failed providers preserving search availability, concurrent edits, graph trust, stale review rejection, schema scope, typed inventory filtering, template editing, changed-source invalidation and migration upgrade/downgrade with an existing row. Browser QA uses an isolated seeded document and a 390px viewport for review layout and confirmation behavior.

Final validation: 290 API tests passed, one integration test skipped; web build and all four existing web tests passed; Ruff and whitespace checks passed. The new metadata regression suite contains 11 tests. Provider extraction was mocked, and the migration was exercised on SQLite; PostgreSQL/provider behavior still requires the corresponding deployment integration checks.
