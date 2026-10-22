# Ingestion API

API สำหรับให้ระบบภายนอก (DMS, ERP, สคริปต์ sync) ส่งเอกสารเข้า Knowledge Base แบบ
machine-to-machine โดยไม่ต้องผ่านเบราว์เซอร์ — ยืนยันตัวตนด้วย Bearer token ไม่ใช่ session cookie

Base path คือ `/api/v1/ingest` เอกสารที่ส่งเข้ามาจะเข้าคิวประมวลผลชุดเดียวกับที่อัปโหลดผ่าน UI
(extract → chunk → embed → index) จึงค้นเจอผ่าน `POST /api/v1/query` และ MCP ได้ทันทีที่ประมวลผลเสร็จ

## Authentication

ทุก request ต้องส่ง header

```
Authorization: Bearer skik_live_xxxxxxxxxxxxxxxxxxxx
```

Token สร้างจากหน้า **Ingest API** ในระบบ — เลือก **Knowledge Base เดียว** ที่ token ใบนี้เขียนได้จาก selector
ในฟอร์ม ระบบเซ็ต scope `documents:write` ให้อัตโนมัติ ไม่มีตัวเลือกอื่นให้ติ๊ก

หน้า **Ingest API** แยกเป็นเมนูอิสระจากหน้า **MCP Tokens** โดยสิ้นเชิง และ backend บังคับ mutual exclusion:
token ใบหนึ่งจะมี MCP tool (`allowed_tools`) พร้อมกับสิทธิ์เขียน Ingest ไม่ได้อีกต่อไป — token ที่สร้างจากหน้า
**Ingest API** จึงเป็น **write-only เสมอ** (ไม่มี MCP tool ใดเลย อ่านเนื้อหาใน Knowledge Base ผ่าน `/mcp`
ไม่ได้) และเขียนได้ **เพียง 1 Knowledge Base ต่อ token เสมอ**

การเลือก Knowledge Base เป็น opt-in แบบชัดเจน ไม่มี wildcard: สร้าง token โดยไม่เลือก Knowledge Base ไม่ได้

secret จะแสดงครั้งเดียวตอนสร้างหรือตอน rotate เท่านั้น ระบบเก็บเฉพาะ HMAC-SHA256 digest
เก็บ secret ไว้ใน secret manager หรือตัวแปรสภาพแวดล้อม (`SOFTNIX_INGEST_TOKEN`) ห้าม commit ลง repo

## รู้ Knowledge Base ที่เขียนได้

`GET /api/v1/ingest/knowledge-bases`

ไม่ต้อง hardcode `KB_ID` ไว้ล่วงหน้าก็ได้ — เรียก endpoint นี้เพื่ออ่านว่า token ใบนี้เขียนได้
Knowledge Base ไหน คืน **0 หรือ 1 รายการเท่านั้นเสมอ** (ไม่ใช่ directory ของทุก Knowledge Base
ในระบบ) เพราะ token หนึ่งใบเขียนได้ Knowledge Base เดียวตามที่อธิบายไว้ใน
[Authentication](#authentication)

```bash
curl "https://knowledge.example.com/api/v1/ingest/knowledge-bases" \
  -H "Authorization: Bearer $SOFTNIX_INGEST_TOKEN"
```

```json
{"items": [{"id": "b17a...", "code": "supply-chain", "name": "Supply Chain Contracts", "status": "active"}]}
```

`items` เป็น list ว่าง (`[]`) เมื่อ Knowledge Base ที่ token นี้ผูกไว้ถูกลบไปแล้ว
ส่วน Knowledge Base ที่ยังไม่ได้ activate (`status` ไม่ใช่ `active`) จะยังปรากฏใน `items`
เพื่อให้ client แยก "ยังไม่ตั้งค่าอะไรเลย" ออกจาก "ตั้งค่าไว้แล้วแต่ถูก disable" ได้ — การอัปโหลด
เข้า Knowledge Base ที่ไม่ active จะตอบ `409 KNOWLEDGE_BASE_DISABLED` เหมือนเดิม

## เลือก Document Type และรับ id

`GET /api/v1/ingest/knowledge-bases/{kb_id}/document-types`

เรียก endpoint นี้ก่อนนำเข้าเพื่อรับ `id` ของ Document Type ที่ใช้งานได้ใน Knowledge Base นั้น รวมทั้ง
ชนิดมาตรฐานและชนิดที่ผู้ดูแลสร้างเอง ผลลัพธ์มี `fields` จึงใช้ตรวจชื่อและชนิดของ metadata ที่ส่งใน
`metadata_json` ได้โดยไม่ต้อง hardcode ฟอร์มไว้ในระบบภายนอก

```bash
curl "https://knowledge.example.com/api/v1/ingest/knowledge-bases/$KB_ID/document-types" \
  -H "Authorization: Bearer $SOFTNIX_INGEST_TOKEN"
```

```json
{
  "items": [{
    "id": "7a1c...",
    "code": "supplier-invoice",
    "name": "Supplier invoice",
    "base_document_type": "general",
    "fields": [
      {"key": "invoice_no", "label": "Invoice number", "field_type": "text"},
      {"key": "categories", "label": "Categories", "field_type": "multi_select", "options": ["ประกาศ", "ที่ดิน"]}
    ]
  }]
}
```

ฟิลด์ `multi_select` รับค่าเป็น JSON array ของ string โดยทุกค่าต้องตรงกับ `options`
ที่ Document Type กำหนด เช่น `{"categories":["ประกาศ", "ที่ดิน"]}`

ใช้ค่า `id` กับ `document_type_id` ในการนำเข้า ตัวอย่างชนิดมาตรฐานคือ `system:general`,
`system:legal`, `system:regulation` และ `system:contract` ส่วนชนิดแบบกำหนดเองใช้ id ที่ endpoint นี้คืนมา

## ตรวจข้อจำกัดก่อนส่ง

`GET /api/v1/ingest/limits`

อ่านข้อจำกัดของระบบและของ token จาก endpoint นี้ แทนการ hardcode ค่าไว้ในระบบต้นทาง
แล้วตรวจไฟล์ก่อนอัปโหลด ไฟล์ที่เกิน `file.max_bytes` จะได้ `413 FILE_TOO_LARGE` หลังจากอัปโหลดจนจบ
จึงควรตรวจขนาดก่อนส่ง และตั้งจำนวนงานที่ส่งพร้อมกันไม่ให้เกิน `token.max_concurrent_requests`

```json
{
  "file": {"max_bytes": 314572800, "extensions": [".csv", ".doc", ".docx", ".htm", ".html", ".json", ".md", ".pdf", ".pptx", ".txt", ".xls", ".xlsx"],
           "mime_types": {".md": ["text/markdown", "text/plain"], "...": []}, "octet_stream_accepted": true},
  "reference_file": {"max_bytes": 314572800, "extensions": [".doc", ".docx", ".pdf", ".txt", ".xls", ".xlsx"], "document_extension": ".md"},
  "text": {"max_bytes": 20971520, "title_max_chars": 255},
  "batch": {"max_files": 20},
  "metadata_json": {"max_chars": 100000},
  "external_id": {"max_chars": 200},
  "lookup": {"max_items": 100},
  "document_list": {"max_limit": 100},
  "token": {"knowledge_base_id": "b17a...", "requests_per_minute": 600, "max_concurrent_requests": 10, "expires_at": null},
  "polling": {"initial_seconds": 5, "max_seconds": 60}
}
```

ข้อจำกัดนี้เป็นของระบบเราเท่านั้น ถ้าเรียกผ่าน proxy หรือ CDN ที่อยู่หน้าระบบ อาจมีเพดานขนาด request ของตัวเองที่ต่ำกว่า
เช่น Cloudflare ปฏิเสธ request ที่ใหญ่กว่า 100 MB ด้วย `413` ที่เป็นหน้า HTML ก่อนถึงระบบ ซึ่งจะไม่มีบันทึกฝั่งเรา

`GET /api/v1/ingest/error-codes` คืนรหัสข้อผิดพลาดทั้งหมดที่ผู้ส่งอาจเจอ ทั้งคำตอบ HTTP ตอนส่ง (`stage: "request"`)
และ `error_code` ของเอกสารหรือ job หลังได้ `202` แล้ว (`stage: "processing"`) แต่ละรหัสบอกว่า

| Field | ความหมาย |
|---|---|
| `http_status` | HTTP status ของรหัสนั้น (`null` สำหรับรหัสระหว่างประมวลผล) |
| `retryable` | ส่งคำขอเดิมหรือรอเอกสารเดิมแล้วสำเร็จได้โดยไม่ต้องแก้อะไร |
| `client_action` | สิ่งที่ผู้ส่งควรทำ: `resend_later` (รอแล้วส่งคำขอเดิมใหม่), `skip` (มีอยู่แล้ว ข้ามได้), `fix_request` (แก้พารามิเตอร์), `fix_file` (แก้ไฟล์), `wait` (ระบบลองใหม่หรือรอเอง ไม่ต้องส่งซ้ำ), `contact_admin` (ต้องให้ผู้ดูแลระบบจัดการ) |

```json
{"items": [
  {"code": "FILE_DUPLICATE", "stage": "request", "http_status": 409, "retryable": false, "client_action": "skip", "description": "..."},
  {"code": "RETRIEVAL_ENGINE_BUSY", "stage": "processing", "http_status": null, "retryable": true, "client_action": "wait", "description": "..."}
]}
```

รหัสที่ไม่อยู่ในรายการให้ถือเป็น `contact_admin`

## ตรวจเอกสารซ้ำก่อนอัปโหลด

`POST /api/v1/ingest/knowledge-bases/{kb_id}/documents/lookup` — `application/json`

ค้นเอกสารที่มีอยู่แล้วใน Knowledge Base ด้วย SHA-256 ของเนื้อหา หรือด้วย `external_id` ได้รวมไม่เกิน 100 ค่าต่อคำขอ
ใช้ในสองกรณี: ก่อนอัปโหลดไฟล์ใหญ่ (ไม่ต้องส่งไฟล์ 160 MB เพื่อได้ `409 FILE_DUPLICATE` กลับมา)
และเมื่อส่งไปแล้วไม่ได้รับ response เพื่อหา `document_id` ของไฟล์นั้น

checksum คือ SHA-256 ของ byte ของไฟล์ที่ส่งใน `file` ส่วนเอกสารที่นำเข้าเป็นข้อความคือ SHA-256 ของ `text` ที่เข้ารหัสแบบ UTF-8
ระบบเทียบกับไฟล์หลักเท่านั้น ไม่เทียบกับไฟล์อ้างอิง และนับเฉพาะเอกสารที่ยังไม่ถูกลบ

```bash
curl -X POST "https://knowledge.example.com/api/v1/ingest/knowledge-bases/$KB_ID/documents/lookup" \
  -H "Authorization: Bearer $SOFTNIX_INGEST_TOKEN" -H "Content-Type: application/json" \
  -d '{"checksums_sha256": ["'"$(shasum -a 256 land-law.md | cut -d' ' -f1)"'"], "external_ids": ["DMS-2569-0042"]}'
```

```json
{"items": [
  {"checksum_sha256": "9f86d0...", "document": {"document_id": "6f2c...", "status": "completed", "...": "..."}},
  {"external_id": "DMS-2569-0042", "document": null}
]}
```

`document` มีโครงเดียวกับ[สถานะเอกสารรายฉบับ](#สถานะเอกสารรายฉบับ) หรือเป็น `null` เมื่อไม่พบ ลำดับของ `items` ตรงกับที่ส่งไป
(checksum ก่อน แล้วตามด้วย external_id) checksum ที่ไม่ใช่เลขฐานสิบหก 64 ตัว หรือจำนวนค่าเป็น 0 หรือเกิน 100 ได้ `400 LOOKUP_INVALID`

### external_id

ส่ง `external_id` (ไม่เกิน 200 ตัวอักษร) เป็นรหัสของเอกสารในระบบต้นทาง เช่นเลขที่เอกสารใน DMS ได้กับการนำเข้าทีละไฟล์และการนำเข้าข้อความ
Knowledge Base หนึ่งมีเอกสารที่ยังไม่ถูกลบได้เพียงฉบับเดียวต่อ `external_id` ส่งซ้ำจะได้ `409 EXTERNAL_ID_EXISTS`
พร้อม `error.document_id` ของฉบับเดิม แม้เนื้อหาจะเปลี่ยนไป ถ้าต้องการแทนฉบับเดิม ให้ลบฉบับเดิมก่อนแล้วส่งใหม่
เอกสารที่ถูกลบแล้วคืน `external_id` ให้ใช้ใหม่ได้ batch endpoint ไม่รับ `external_id`

## นำเข้าเอกสารทีละไฟล์

`POST /api/v1/ingest/knowledge-bases/{kb_id}/documents` — `multipart/form-data`

| Field | ต้องมี | คำอธิบาย |
|---|---|---|
| `file` | ✓ | ไฟล์เอกสาร |
| `title` | | ชื่อเอกสาร ถ้าไม่ส่งจะใช้ชื่อไฟล์ |
| `document_type_id` | | id จาก endpoint Document Type; เมื่อระบุ ระบบใช้ processing profile และ metadata fields ของชนิดนั้น |
| `document_type` | | ทางเลือกเดิมสำหรับชนิดมาตรฐาน (`general`, `legal`, `regulation`, `contract`) เมื่อไม่ได้ส่ง `document_type_id` |
| `template_id` | | ชื่อเดิมที่ยังรองรับเพื่อความเข้ากันได้ ใช้แทน `document_type_id` เท่านั้น |
| `metadata_json` | | JSON object ของค่า metadata ตาม template |
| `published_at` | | วันที่ประกาศใช้ รูปแบบ `YYYY-MM-DD` |
| `reference_file` | | ไฟล์อ้างอิงสำหรับ `.md` รองรับ PDF, DOCX, DOC, XLSX, XLS และ TXT เก็บกับเอกสารเดียวกันโดยไม่ OCR หรือสร้างดัชนีซ้ำ |
| `reference_pdf` | | ฟิลด์เดิมสำหรับ PDF; ยังใช้ได้เพื่อความเข้ากันได้ ห้ามส่งพร้อม `reference_file` |
| `external_id` | | รหัสเอกสารของระบบต้นทาง ไม่เกิน 200 ตัวอักษร ดู [external_id](#external_id) |

```bash
curl -X POST "https://knowledge.example.com/api/v1/ingest/knowledge-bases/$KB_ID/documents" \
  -H "Authorization: Bearer $SOFTNIX_INGEST_TOKEN" \
  -F "file=@./contract.pdf" \
  -F "title=สัญญาจัดซื้อ 2569" \
  -F "document_type_id=system:contract" \
  -F "published_at=2026-01-15"
```

สำหรับฟิลด์ `multi_select` ให้ส่ง `metadata_json` เป็น JSON object เช่น
`-F 'metadata_json={"categories":["ประกาศ","ที่ดิน"]}'`
API ปฏิเสธค่าที่ไม่ใช่ array, ตัวเลือกที่ไม่มีใน `options` หรือค่าซ้ำใน array

หากมีข้อความ Markdown ที่ OCR มาแล้วและต้องการเก็บไฟล์ต้นฉบับเป็นหลักฐาน ให้ส่งทั้งสองไฟล์ใน request เดียว:

```bash
curl -X POST "https://knowledge.example.com/api/v1/ingest/knowledge-bases/$KB_ID/documents" \
  -H "Authorization: Bearer $SOFTNIX_INGEST_TOKEN" \
  -F "file=@./land-law.md;type=text/markdown" \
  -F "reference_file=@./land-law.pdf;type=application/pdf" \
  -F "document_type_id=$LEGAL_DOCUMENT_TYPE_ID"
```

เปลี่ยนชื่อไฟล์และ MIME ใน `reference_file` เป็น DOCX, DOC, XLSX, XLS หรือ TXT ได้ตามต้องการ ระบบอ่านและสกัด metadata จาก `.md` เท่านั้น ไฟล์อ้างอิงไม่ผ่าน OCR/chunk/embed/index และถูกส่งเป็น `references[].file` ในคำตอบ MCP โดยมี `role=reference_file` (PDF ยังคง `role=reference_pdf` เพื่อความเข้ากันได้) ไฟล์ Markdown อยู่ใน `references[].content_file` อย่าอัปโหลดไฟล์อ้างอิงเป็น `file` อีก request เพราะจะกลายเป็นเอกสารอีกชิ้นที่เข้ากระบวนการตามชนิดไฟล์

ตอบ **`202 Accepted`** เพราะงานเพียงเข้าคิว ยังประมวลผลไม่เสร็จ

```json
{
  "status": "queued",
  "document_id": "6f2c...",
  "job_id": "a91d...",
  "external_id": null,
  "document_type": "contract",
  "document_type_id": "system:contract",
  "template_id": "system:contract"
}
```

## นำเข้าหลายไฟล์ในครั้งเดียว

`POST /api/v1/ingest/knowledge-bases/{kb_id}/documents/batch` — `multipart/form-data`
ส่งฟิลด์ `files` ซ้ำได้ **สูงสุด 20 ไฟล์ต่อ request** (เกินจะได้ `400 BATCH_TOO_MANY_FILES`)

`document_type_id`, `document_type`, `template_id`, `metadata_json` ใช้ร่วมกันทั้ง batch — ถ้าต้องการ metadata ต่างกันรายไฟล์
ให้ใช้ endpoint แบบไฟล์เดียว

```bash
curl -X POST "https://knowledge.example.com/api/v1/ingest/knowledge-bases/$KB_ID/documents/batch" \
  -H "Authorization: Bearer $SOFTNIX_INGEST_TOKEN" \
  -F "files=@./a.pdf" \
  -F "files=@./b.docx" \
  -F "document_type_id=system:general"
```

**Partial success:** ไฟล์เสียใบเดียวไม่ทำให้ทั้ง batch ตก ระบบจะเข้าคิวไฟล์ที่ผ่านทั้งหมดแล้วรายงานผลรายไฟล์
`status` เป็น `queued` เมื่อสำเร็จทุกไฟล์ และเป็น `partial` เมื่อมีไฟล์ตกอย่างน้อยหนึ่งไฟล์
ทั้งสองกรณี HTTP status คือ `202` — **ต้องอ่าน `results[]` เสมอ อย่าดูแค่ HTTP status**

```json
{
  "status": "partial",
  "total": 2,
  "queued_count": 1,
  "failed_count": 1,
  "document_type": "general",
  "document_type_id": "system:general",
  "template_id": "system:general",
  "results": [
    {"filename": "a.pdf", "status": "queued", "document_id": "6f2c...", "job_id": "a91d...",
     "document_type": "general", "document_type_id": "system:general", "template_id": "system:general"},
    {"filename": "b.exe", "status": "failed", "error_code": "FILE_TYPE_NOT_SUPPORTED",
     "message": "Upload rejected", "document_type": "general", "document_type_id": "system:general", "template_id": "system:general"}
  ]
}
```

## นำเข้าข้อความโดยตรง (JSON Payload)

`POST /api/v1/ingest/knowledge-bases/{kb_id}/documents/text` — `application/json`

สำหรับระบบ Machine-to-Machine ที่ทำการสกัดข้อความ (OCR/LLM) มาแล้ว เช่น InsightDOC Custom API หรือ pipeline ภายนอก โดยไม่ต้องส่งไฟล์ไบนารี ข้อความจะถูกจัดเก็บเป็น Markdown document และเข้าสู่ pipeline ปกติ (chunk, embed, index)

ถ้าฝั่งส่งข้อมูลใช้ JSON endpoint นี้และมีไฟล์หลักฐาน ให้เรียก `POST /api/v1/ingest/documents/{document_id}/reference-file` หลังได้รับ `document_id` โดยส่ง `multipart/form-data` ฟิลด์ `file` ด้วย ingest token เดิม รองรับไฟล์อ้างอิงหนึ่งไฟล์ต่อเอกสารและไม่ประมวลผลซ้ำ ดาวน์โหลดผ่าน `GET /api/v1/ingest/documents/{document_id}/file?variant=reference_file` หรือ MCP read token ใช้ `GET /api/v1/documents/{document_id}/file?variant=reference_file` เส้นทางเดิม `reference-pdf` และ `variant=reference_pdf` ยังใช้ได้สำหรับ PDF

| Field | ต้องมี | คำอธิบาย |
|---|---|---|
| `title` | ✓ | ชื่อเอกสาร |
| `text` | ✓ | ข้อความเนื้อหาเอกสาร (Markdown / Plain Text) |
| `document_type_id` | | id ของ Document Type จาก endpoint Document Type |
| `document_type` | | ทางเลือกเดิมสำหรับชนิดมาตรฐาน เมื่อไม่ได้ส่ง `document_type_id` |
| `template_id` | | ชื่อเดิมที่ยังรองรับ ใช้แทน `document_type_id` เท่านั้น |
| `metadata_json` | | JSON string ของ metadata (ทางเลือก) |
| `published_at` | | วันที่ประกาศใช้ รูปแบบ `YYYY-MM-DD` (ทางเลือก) |
| `external_id` | | รหัสเอกสารของระบบต้นทาง ไม่เกิน 200 ตัวอักษร (ทางเลือก) ดู [external_id](#external_id) |

```bash
curl -X POST "https://knowledge.example.com/api/v1/ingest/knowledge-bases/$KB_ID/documents/text" \
  -H "Authorization: Bearer $SOFTNIX_INGEST_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "สัญญาจัดซื้อ 2569",
    "text": "# สัญญาจัดซื้อ 2569\nรายละเอียดข้อตกลงและเงื่อนไข...",
    "document_type_id": "system:contract"
  }'
```

ตอบ **`202 Accepted`** โครงสร้างผลลัพธ์เหมือนกับการอัปโหลดไฟล์:

```json
{
  "status": "queued",
  "document_id": "6f2c...",
  "job_id": "a91d...",
  "document_type": "contract",
  "document_type_id": "system:contract",
  "template_id": "system:contract"
}
```

## ติดตามสถานะ

### สถานะเอกสารรายฉบับ

`GET /api/v1/ingest/documents/{document_id}`

```json
{
  "document_id": "6f2c...",
  "knowledge_base_id": "b17a...",
  "external_id": "DMS-2569-0042",
  "title": "สัญญาจัดซื้อ 2569",
  "filename": "contract.pdf",
  "checksum_sha256": "9f86d0...",
  "status": "queued",
  "document_type": "contract",
  "document_type_id": "system:contract",
  "error_code": null,
  "error_message": null,
  "retryable": null,
  "client_action": null,
  "created_at": "2026-08-03T04:12:55",
  "updated_at": "2026-08-03T04:13:40",
  "indexed_at": null,
  "waiting_for": "RETRIEVAL_ENGINE_BUSY",
  "next_attempt_at": "2026-08-03T04:14:10Z",
  "latest_job": {
    "id": "a91d...",
    "type": "PROCESS_DOCUMENT",
    "status": "queued",
    "stage": "indexing",
    "progress_percent": 60,
    "attempt_count": 1,
    "error_code": "RETRIEVAL_ENGINE_BUSY",
    "error_message": "...",
    "waiting_for": "RETRIEVAL_ENGINE_BUSY",
    "next_attempt_at": "2026-08-03T04:14:10Z"
  }
}
```

**`status` ของเอกสาร** คือสิ่งที่ควรใช้ตัดสินว่างานจบหรือยัง — สถานะสุดท้ายคือ `completed`,
`failed` หรือ `ocr_required` (เกิดเฉพาะเมื่อปิด OCR chain ทั้งหมด ให้ตั้งค่าแล้วสั่ง reprocess เอง
ระบบจะไม่ retry ให้)
ส่วน `queued`/`extracting` คือกำลังทำงานอยู่ และ `latest_job.stage`/`progress_percent` มีไว้แสดง
ความคืบหน้าระหว่างทางเท่านั้น อย่าใช้เป็นเงื่อนไขว่างานเสร็จ

`attempt_count` มากกว่า 1 หมายถึงงานถูก retry อัตโนมัติแบบ exponential backoff ยังไม่ถือว่าล้มเหลว
เมื่อ `status` เป็น `failed` ให้ดู `error_code` ประกอบ เช่น `OCR_CHAIN_FAILED` (ทุก engine ใน
chain ล้ม — ดู log ของ worker ว่า Softnix/Mistral/Tesseract ตัวไหนพร้อมใช้) `OCR_REQUIRED`
(ปิด chain ไว้ทั้งหมด) หรือ `TEXT_EXTRACTION_FAILED`

field ที่ช่วยแยก "รออยู่" ออกจาก "ต้องลงมือ":

| Field | ความหมาย |
|---|---|
| `waiting_for` | รหัสที่ job ล่าสุดกำลังรออยู่ เช่น `RETRIEVAL_ENGINE_BUSY` หรือ `LLM_CREDITS_EXHAUSTED` เป็น `null` เมื่อไม่ได้รออะไร ระบบลองใหม่หรือทำต่อเอง ไม่ต้องส่งซ้ำ |
| `next_attempt_at` | เวลา UTC ที่ job ล่าสุดจะเริ่มอีกครั้ง เป็น `null` เมื่อพร้อมทำทันทีหรือไม่ได้อยู่ในคิว |
| `retryable` / `client_action` | มีค่าเฉพาะเอกสาร `failed` ตาม[แค็ตตาล็อกรหัสข้อผิดพลาด](#ตรวจข้อจำกัดก่อนส่ง) |
| `checksum_sha256` | SHA-256 ของเนื้อหา ใช้กับ [lookup](#ตรวจเอกสารซ้ำก่อนอัปโหลด) |
| `updated_at` / `indexed_at` | เวลาที่แถวเอกสารเปลี่ยนล่าสุด และเวลาที่เข้าดัชนีค้นหาเสร็จ (เวลา UTC ไม่มี `Z` เหมือน `created_at`) |

### Active Queue และสถานะการประมวลผล

`GET /api/v1/ingest/knowledge-bases/{kb_id}/queue`

บอกจำนวนเอกสารที่ค้างอยู่ใน Active Queue (job ที่ `queued` หรือ `running`) ของทั้งระบบ
และของ Knowledge Base นี้ รวมทั้งบอกว่าระบบหยุดประมวลผลชั่วคราวหรือไม่ ใช้เพื่อชะลอหรือหยุดส่งเมื่อคิวยาว
แทนการส่งต่อเรื่อย ๆ แล้ว poll รายเอกสาร

```json
{
  "knowledge_base_id": "b17a...",
  "generated_at": "2026-10-22T08:30:00Z",
  "processing": "paused",
  "holds": [{"reason_code": "LLM_CREDITS_EXHAUSTED", "held_since": "2026-10-22T08:10:02Z", "next_check_at": "2026-10-22T08:32:02Z"}],
  "active_queue": {
    "documents": 148,
    "jobs": {"queued": 160, "running": 3, "waiting_to_retry": 12}
  },
  "knowledge_base": {
    "active_documents": 24,
    "jobs": {"queued": 25, "running": 1, "waiting_to_retry": 2},
    "oldest_active_job_at": "2026-10-22T07:58:41Z",
    "documents_by_status": {"completed": 310, "queued": 23, "extracting": 1, "failed": 2},
    "in_flight_documents": 24
  }
}
```

| Field | ความหมาย |
|---|---|
| `processing` | `running` หรือ `paused` เมื่อ AI provider หมดเครดิตหรือ key ใช้ไม่ได้ งานที่ส่งเข้ามายังได้ `202` และอยู่ในคิวถาวร แต่จะเริ่มเมื่อ hold ถูกยกเลิก ซึ่งระบบตรวจเองทุก 1–10 นาที |
| `holds[]` | สาเหตุที่หยุด (`reason_code` ตามแค็ตตาล็อก) เวลาที่เริ่มหยุด และเวลาที่จะตรวจอีกครั้ง |
| `active_queue.documents` | จำนวนเอกสาร (ไม่ซ้ำ) ที่มีงานค้างใน Active Queue ของทุก Knowledge Base แสดงเป็นตัวเลขเท่านั้น ไม่มีชื่อหรือ id ของ Knowledge Base อื่น |
| `active_queue.jobs` | จำนวน job ที่ `queued` และ `running` โดย `waiting_to_retry` คือ job ที่อยู่ในคิวแต่รอถึง `next_attempt_at` (เช่นรอ LightRAG ว่าง) |
| `knowledge_base.active_documents` | จำนวนเอกสารของ Knowledge Base นี้ที่มีงานค้างในคิว รวมงานต่อเนื่องเช่นสกัด metadata ของเอกสารที่ `completed` แล้ว |
| `knowledge_base.in_flight_documents` | จำนวนเอกสารที่ยังไม่ถึงสถานะสุดท้าย (`completed`, `failed`, `ocr_required`) |
| `knowledge_base.oldest_active_job_at` | เวลาที่ job เก่าที่สุดที่ยังค้างของ Knowledge Base นี้ถูกสร้าง ถ้าห่างจากปัจจุบันมาก แปลว่าคิวเดินช้า |

### ประวัติ job ของเอกสาร

`GET /api/v1/ingest/documents/{document_id}/jobs` — คืน array ของ job เรียงใหม่สุดก่อน
ใช้เมื่อต้องการดูรอบ retry ทั้งหมด

### ลิสต์เอกสารใน Knowledge Base

`GET /api/v1/ingest/knowledge-bases/{kb_id}/documents?limit=50&offset=0`

`status` เป็น query parameter ทางเลือกและเป็น exact match ค่าเดียว (เช่น `?status=failed`)
ไม่ใช่ "in-flight" — ไม่มีสถานะเดียวที่แทนทั้ง `queued`+`extracting` ได้ ถ้าต้องการดูงานที่ยัง
ไม่จบ ให้เรียกแบบไม่ส่ง `status` เลยแล้วกรอง `completed`/`failed`/`ocr_required` ออกที่ฝั่ง client
`limit` รับค่า 1–100 (ค่าเริ่มต้น 50) `offset` ต้องไม่ติดลบ ผิดเงื่อนไขได้ `400 DOCUMENT_PAGE_INVALID`
คืน `{items, total, limit, offset}` โดยแต่ละ item มีโครงเดียวกับสถานะเอกสารรายฉบับ

### ลบเอกสาร

`DELETE /api/v1/ingest/documents/{document_id}`

ลบแบบ **soft delete**: เอกสารจะหายจากรายการและจาก API ของ Ingest token ทันที แต่ยังสามารถกู้คืนได้
จากหน้าจัดการเอกสารของผู้ดูแลระบบ ระบบจะยกเลิก job ที่ยัง `queued` หรือ `running` และสร้าง job
`PURGE_REMOTE_INDEX` เพื่อลบ source ออกจาก retrieval index จึงไม่เหลือผลค้นหาจากเอกสารที่ถูกลบ

Token ลบได้เฉพาะเอกสารใน Knowledge Base เดียวที่ผูกไว้เท่านั้น เอกสารนอกขอบเขตหรือที่ถูกลบแล้วจะตอบ
`404 DOCUMENT_NOT_FOUND` เพื่อไม่เปิดเผยข้อมูลข้ามคลังความรู้

```bash
curl -X DELETE "https://knowledge.example.com/api/v1/ingest/documents/$DOCUMENT_ID" \
  -H "Authorization: Bearer $SOFTNIX_INGEST_TOKEN"
```

```json
{
  "status": "deleted",
  "document_id": "6f2c...",
  "purge_job_id": "a91d..."
}
```

### จังหวะการ poll ที่แนะนำ

เริ่มที่ **5 วินาที** แล้ว backoff เป็นสองเท่าจนไม่เกิน 60 วินาที และหยุดเมื่อ `status` เป็นสถานะสุดท้าย
ถ้าส่งเป็น batch ให้ใช้ `GET .../knowledge-bases/{kb_id}/documents` (ไม่ส่ง `status`) เพื่อ poll
ครั้งเดียวต่อรอบแล้วกรองที่ client แทนการยิงรายเอกสาร เพราะทุก request ถูกหักจาก rate limit ของ
token ใบเดียวกัน

ระหว่างนำเข้าชุดใหญ่ ให้เรียก `GET .../knowledge-bases/{kb_id}/queue` ก่อนส่งแต่ละรอบ ถ้า `processing` เป็น `paused`
ให้หยุดส่งชั่วคราวจนกว่าจะกลับเป็น `running` และถ้า `knowledge_base.in_flight_documents` สูง ให้ชะลอการส่ง
เอกสารที่มี `waiting_for` ไม่ต้องส่งซ้ำ ระบบทำต่อเอง

## ตัวอย่าง Python

```python
import os, time, requests

API_BASE = "https://knowledge.example.com/api/v1"
KB_ID = os.environ["SOFTNIX_KB_ID"]
HEADERS = {"Authorization": f"Bearer {os.environ['SOFTNIX_INGEST_TOKEN']}"}
TERMINAL = {"completed", "failed", "ocr_required"}


def upload(path, document_type="general", title=None):
    with open(path, "rb") as handle:
        response = requests.post(
            f"{API_BASE}/ingest/knowledge-bases/{KB_ID}/documents",
            headers=HEADERS,
            files={"file": (os.path.basename(path), handle)},
            data={"document_type": document_type, **({"title": title} if title else {})},
            timeout=120,
        )
    if response.status_code == 409 and response.json()["error"]["code"] == "FILE_DUPLICATE":
        return None  # ส่งซ้ำ ไม่ต้องทำอะไร
    response.raise_for_status()
    return response.json()["document_id"]


def wait_for(document_id, interval=5, max_interval=60):
    while True:
        document = requests.get(f"{API_BASE}/ingest/documents/{document_id}",
                                headers=HEADERS, timeout=30).json()
        if document["status"] in TERMINAL:
            return document
        time.sleep(interval)
        interval = min(interval * 2, max_interval)


document_id = upload("./contract.pdf", "contract", "สัญญาจัดซื้อ 2569")
if document_id:
    result = wait_for(document_id)
    print(result["status"], result.get("error_code") or "")
```

## ตัวอย่าง Node.js

```javascript
import {readFile} from "node:fs/promises";
import {basename} from "node:path";

const API_BASE = "https://knowledge.example.com/api/v1";
const KB_ID = process.env.SOFTNIX_KB_ID;
const HEADERS = {Authorization: `Bearer ${process.env.SOFTNIX_INGEST_TOKEN}`};
const TERMINAL = new Set(["completed", "failed", "ocr_required"]);

async function upload(path, documentType = "general", title) {
  const body = new FormData();
  body.append("file", new Blob([await readFile(path)]), basename(path));
  body.append("document_type", documentType);
  if (title) body.append("title", title);
  const response = await fetch(`${API_BASE}/ingest/knowledge-bases/${KB_ID}/documents`,
    {method: "POST", headers: HEADERS, body});
  if (response.status === 409) return null; // FILE_DUPLICATE
  if (!response.ok) throw new Error(JSON.stringify(await response.json()));
  return (await response.json()).document_id;
}

async function waitFor(documentId, interval = 5000, maxInterval = 60000) {
  for (;;) {
    const response = await fetch(`${API_BASE}/ingest/documents/${documentId}`, {headers: HEADERS});
    const document = await response.json();
    if (TERMINAL.has(document.status)) return document;
    await new Promise(resolve => setTimeout(resolve, interval));
    interval = Math.min(interval * 2, maxInterval);
  }
}

const documentId = await upload("./contract.pdf", "contract", "สัญญาจัดซื้อ 2569");
if (documentId) console.log(await waitFor(documentId));
```

## Error contract

ทุก error ใช้รูปแบบเดียวกับ REST API ส่วนอื่น

```json
{"status": "error", "error": {"code": "AUTH_SCOPE_NOT_ALLOWED", "message": "...", "retryable": false}}
```

| HTTP | Code | ความหมาย | `retryable` |
|---|---|---|---|
| 401 | `AUTH_TOKEN_MISSING` | ไม่ได้ส่ง header `Authorization: Bearer` | false |
| 401 | `AUTH_TOKEN_INVALID` | token ไม่ถูกต้องหรือถูกปิดใช้งาน | false |
| 401 | `AUTH_TOKEN_REVOKED` | token ถูกเพิกถอนแล้ว | false |
| 401 | `AUTH_TOKEN_EXPIRED` | token หมดอายุ | false |
| 403 | `AUTH_SCOPE_NOT_ALLOWED` | token ไม่มี scope `documents:write` | false |
| 403 | `AUTH_KNOWLEDGE_BASE_NOT_ALLOWED` | token ไม่ได้ตั้งค่า Knowledge Base สำหรับ ingest ไว้ (ผ่านการตรวจตอนสร้าง token แล้วตามปกติ — เจอกรณีนี้ได้เฉพาะ token ที่ออกไว้ก่อนอัปเดตนี้) | false |
| 400 | `INGEST_KNOWLEDGE_BASE_REQUIRED` | เปิด scope `documents:write` แต่ไม่ได้เลือก Knowledge Base สำหรับ ingest ตอนสร้าง/rotate token | false |
| 400 | `INGEST_KNOWLEDGE_BASE_NOT_ALLOWED` | ระบุ Knowledge Base สำหรับ ingest มาโดยไม่ได้เปิด scope `documents:write` | false |
| 404 | `KNOWLEDGE_BASE_NOT_FOUND` | ไม่พบ Knowledge Base **หรืออยู่นอกสิทธิ์ของ token** | false |
| 404 | `DOCUMENT_NOT_FOUND` | ไม่พบเอกสาร **หรืออยู่นอกสิทธิ์ของ token** | false |
| 409 | `KNOWLEDGE_BASE_DISABLED` | Knowledge Base ยังไม่ถูก activate | false |
| 409 | `FILE_DUPLICATE` | ไฟล์เนื้อหาเดียวกันมีอยู่ใน Knowledge Base แล้ว (`error.document_id` คือฉบับเดิม) | false |
| 409 | `EXTERNAL_ID_EXISTS` | มีเอกสาร `external_id` เดียวกันอยู่แล้ว (`error.document_id` คือฉบับเดิม) | false |
| 400 | `EXTERNAL_ID_INVALID` | `external_id` ยาวเกิน 200 ตัวอักษร | false |
| 400 | `LOOKUP_INVALID` | lookup ไม่มีค่า มีเกิน 100 ค่า หรือ checksum ไม่ใช่เลขฐานสิบหก 64 ตัว | false |
| 400 | `FILE_TYPE_NOT_SUPPORTED` | นามสกุลไฟล์ไม่รองรับ | false |
| 400 | `FILE_MIME_TYPE_NOT_SUPPORTED` | MIME type ไม่ตรงกับนามสกุลไฟล์ | false |
| 400 | `DOCUMENT_TYPE_INVALID` | `document_type` ไม่ถูกต้อง | false |
| 400 | `DOCUMENT_TEMPLATE_NOT_FOUND` | `template_id` ไม่มีอยู่ใน Knowledge Base นี้ | false |
| 400 | `DOCUMENT_METADATA_INVALID` | `metadata_json` ไม่ใช่ JSON object หรือค่าไม่ตรงชนิดของ field | false |
| 400 | `DOCUMENT_METADATA_FIELD_UNKNOWN` | ส่ง key ที่ไม่ได้ประกาศไว้ใน template | false |
| 400 | `DOCUMENT_METADATA_REQUIRED` | ไม่ได้ส่ง field ที่ template กำหนดว่าจำเป็น | false |
| 400 | `DOCUMENT_METADATA_TOO_LARGE` | `metadata_json` ใหญ่เกินเพดาน | false |
| 400 | `BATCH_FILES_REQUIRED` | batch ไม่มีไฟล์เลย | false |
| 400 | `BATCH_TOO_MANY_FILES` | batch เกิน 20 ไฟล์ | false |
| 400 | `DOCUMENT_PAGE_INVALID` | `limit` ไม่อยู่ในช่วง 1–100 หรือ `offset` ติดลบ | false |
| 413 | `FILE_TOO_LARGE` | ไฟล์ใหญ่เกินขนาดที่ตั้งไว้ | false |
| 429 | `MCP_RATE_LIMITED` | เกิน requests/min ของ token | **true** |
| 429 | `MCP_CONCURRENCY_LIMITED` | เกินจำนวน request พร้อมกันของ token | **true** |
| 503 | `MCP_LIMIT_STORE_UNAVAILABLE` | ตัวนับ rate limit ใช้งานไม่ได้ชั่วคราว | **true** |

**404 ไม่ใช่ 403 โดยเจตนา** — Knowledge Base และเอกสารที่อยู่นอกสิทธิ์ตอบ `404` เหมือนกับที่ไม่มีอยู่จริง
เพื่อไม่ให้ token ใบหนึ่งใช้ผลลัพธ์ต่างกันมาไล่เดา (enumerate) ทรัพยากรของคนอื่น

`FILE_DUPLICATE` ตอบ `409` (ต่างจากหน้าอัปโหลดใน UI ที่ใช้ `400`) เพื่อให้ client ที่ retry แยกได้ว่า
"ส่งซ้ำ" ไม่ใช่ "ส่งผิด" — กรณีนี้ปลอดภัยที่จะข้ามไป ไม่ต้องส่งใหม่ ใช้ `error.document_id` ผูกไฟล์ในระบบต้นทางกับเอกสารเดิมได้
ใน batch รายการที่ซ้ำจะมี `document_id` ใน `results[]` เช่นกัน

ตารางนี้คือรหัสที่ตอบตอนส่ง รหัสทั้งหมดรวมรหัสระหว่างประมวลผลพร้อม `client_action` อ่านได้จาก `GET /api/v1/ingest/error-codes`

เฉพาะ error ที่ `retryable: true` ควร retry ให้หน่วงแบบ exponential backoff อย่าวน retry ทันที

## Rate limit และ concurrency

แต่ละ token มีเพดานของตัวเอง ตั้งค่าได้ที่ **Advanced limits** ตอนสร้าง token
ถ้าเว้นว่างไว้ ingest token จะได้ 600 requests/min และ 10 concurrent requests ส่วน MCP token จะได้ 60 และ 5
เพดานนี้**ใช้ร่วมกันระหว่าง Ingest API กับ `/mcp`** เพราะเป็นงบประมาณของ token ใบเดียวกัน

การอัปโหลดใช้เวลานานกว่าการอ่าน จึงควรจำกัดจำนวน worker ที่อัปโหลดพร้อมกันไม่ให้เกินค่า concurrent
ที่ตั้งไว้ และใช้ batch endpoint เพื่อลดจำนวน request

request ที่เกินเพดานได้ `429` ทันที พร้อม header `Retry-After` (วินาที) ให้รอตามนั้นแล้วส่ง request เดิมใหม่
request ที่ถูกปฏิเสธไม่ถูกนับในโควตา server ไม่ถือ request ค้างไว้รอโควตา เพื่อไม่ให้การส่งเกินเพดานทำให้ API ช้าลงทั้งระบบ
ไฟล์ที่ได้ `202` แล้วอยู่ในคิวถาวร และถูกประมวลผลตามลำดับแม้ api หรือ worker จะ restart

ปรับเพดานของ token เดิมได้โดยไม่ต้องออก secret ใหม่:

```
PATCH /api/v1/tokens/{token_id}/limits
{"requests_per_minute": 600, "max_concurrent_requests": 10}
```

## ชนิดไฟล์และขนาด

รองรับ `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.xls`, `.txt`, `.md`, `.html`, `.htm`, `.csv`, `.json`

MIME type ที่ส่งมาต้องตรงกับนามสกุล (ส่ง `application/octet-stream` ได้ ระบบจะเดาจากนามสกุลให้)
ขนาดสูงสุดกำหนดด้วย `MAX_FILE_SIZE_MB` (ค่าเริ่มต้น 100 MB)

PDF ที่เป็นภาพสแกน (หรือฟอนต์ไทยพัง) จะถูกส่งเข้า OCR chain อัตโนมัติ:
**Softnix OCR → Mistral OCR → Tesseract (tha+eng)** ตามลำดับ — หน้าที่ engine แรก
ล้มหรือค้างเกิน `SOFTNIX_OCR_STALL_SECONDS` (ค่าเริ่มต้น 45 วิ) จะตกไป engine ถัดไป
ทันที ตั้งค่าผ่าน `SOFTNIX_OCR_*`, `MISTRAL_API_KEY` และ `OCR_CHAIN_ENGINES`
รายละเอียดดู [README](../README.md)

## Rotate และ revoke

ที่หน้า **Ingest API**

- **Rotate key** — ออก secret ใหม่และเพิกถอนใบเก่าทันที `allowed_scopes` และสิทธิ์ Knowledge Base
  ถูกคัดลอกมาให้ครบ integration จึงทำงานต่อได้เมื่ออัปเดต secret แล้ว วางแผน rotate ตอนที่หยุด
  ส่งงานได้ เพราะ secret เก่าใช้ไม่ได้ทันที
- **Disable** — หยุดใช้ชั่วคราว เปิดกลับได้
- **Revoke** — เพิกถอนถาวร ใช้เมื่อสงสัยว่า secret รั่ว

การเรียก Ingest API ทุกครั้งถูกบันทึกในหน้า **Logging** โดย transaction log ระบุ
`authentication: ingest_token` และ audit log บันทึก action `document.upload` พร้อม
`transport: ingest_api` และชื่อ token — ใช้ตรวจสอบย้อนหลังได้ว่า integration ใดส่งเอกสารใดเข้ามา
ตัว secret ไม่ถูกบันทึกที่ใดเลย

## อ่านต่อ

- [REST API](API.md) — endpoint ส่วนที่เหลือ
- [MCP](MCP.md) — ฝั่งอ่านข้อมูลสำหรับ AI agent
- [Security](SECURITY.md) — โมเดลการยืนยันตัวตนและสิทธิ์
- [Deployment](DEPLOYMENT.md) — การตั้งค่า production
