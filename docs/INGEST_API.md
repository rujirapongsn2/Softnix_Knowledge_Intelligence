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

## นำเข้าเอกสารทีละไฟล์

`POST /api/v1/ingest/knowledge-bases/{kb_id}/documents` — `multipart/form-data`

| Field | ต้องมี | คำอธิบาย |
|---|---|---|
| `file` | ✓ | ไฟล์เอกสาร |
| `title` | | ชื่อเอกสาร ถ้าไม่ส่งจะใช้ชื่อไฟล์ |
| `document_type` | | `general` (ค่าเริ่มต้น), `legal`, `regulation`, `contract` |
| `template_id` | | id ของ Document Type ที่กำหนดฟอร์ม metadata ไว้ |
| `metadata_json` | | JSON object ของค่า metadata ตาม template |
| `published_at` | | วันที่ประกาศใช้ รูปแบบ `YYYY-MM-DD` |

```bash
curl -X POST "https://knowledge.example.com/api/v1/ingest/knowledge-bases/$KB_ID/documents" \
  -H "Authorization: Bearer $SOFTNIX_INGEST_TOKEN" \
  -F "file=@./contract.pdf" \
  -F "title=สัญญาจัดซื้อ 2569" \
  -F "document_type=contract" \
  -F "published_at=2026-01-15"
```

ตอบ **`202 Accepted`** เพราะงานเพียงเข้าคิว ยังประมวลผลไม่เสร็จ

```json
{
  "status": "queued",
  "document_id": "6f2c...",
  "job_id": "a91d...",
  "document_type": "contract",
  "template_id": null
}
```

## นำเข้าหลายไฟล์ในครั้งเดียว

`POST /api/v1/ingest/knowledge-bases/{kb_id}/documents/batch` — `multipart/form-data`
ส่งฟิลด์ `files` ซ้ำได้ **สูงสุด 20 ไฟล์ต่อ request** (เกินจะได้ `400 BATCH_TOO_MANY_FILES`)

`document_type`, `template_id`, `metadata_json` ใช้ร่วมกันทั้ง batch — ถ้าต้องการ metadata ต่างกันรายไฟล์
ให้ใช้ endpoint แบบไฟล์เดียว

```bash
curl -X POST "https://knowledge.example.com/api/v1/ingest/knowledge-bases/$KB_ID/documents/batch" \
  -H "Authorization: Bearer $SOFTNIX_INGEST_TOKEN" \
  -F "files=@./a.pdf" \
  -F "files=@./b.docx" \
  -F "document_type=general"
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
  "template_id": null,
  "results": [
    {"filename": "a.pdf", "status": "queued", "document_id": "6f2c...", "job_id": "a91d...",
     "document_type": "general", "template_id": null},
    {"filename": "b.exe", "status": "failed", "error_code": "FILE_TYPE_NOT_SUPPORTED",
     "message": "Upload rejected", "document_type": "general", "template_id": null}
  ]
}
```

## ติดตามสถานะ

### สถานะเอกสารรายฉบับ

`GET /api/v1/ingest/documents/{document_id}`

```json
{
  "document_id": "6f2c...",
  "knowledge_base_id": "b17a...",
  "title": "สัญญาจัดซื้อ 2569",
  "filename": "contract.pdf",
  "status": "extracting",
  "document_type": "contract",
  "error_code": null,
  "created_at": "2026-08-03T04:12:55",
  "latest_job": {
    "id": "a91d...",
    "type": "PROCESS_DOCUMENT",
    "status": "running",
    "stage": "embedding",
    "progress_percent": 60,
    "attempt_count": 1,
    "error_code": null,
    "error_message": null
  }
}
```

**`status` ของเอกสาร** คือสิ่งที่ควรใช้ตัดสินว่างานจบหรือยัง — สถานะสุดท้ายคือ `completed`,
`failed` หรือ `ocr_required` (ต้องตั้งค่า OCR แล้วสั่ง reprocess เอง ระบบจะไม่ retry ให้)
ส่วน `queued`/`extracting` คือกำลังทำงานอยู่ และ `latest_job.stage`/`progress_percent` มีไว้แสดง
ความคืบหน้าระหว่างทางเท่านั้น อย่าใช้เป็นเงื่อนไขว่างานเสร็จ

`attempt_count` มากกว่า 1 หมายถึงงานถูก retry อัตโนมัติแบบ exponential backoff ยังไม่ถือว่าล้มเหลว
เมื่อ `status` เป็น `failed` ให้ดู `error_code` ประกอบ เช่น `OCR_REQUIRED` (PDF เป็นภาพสแกน
ต้องตั้งค่า OCR ก่อน) หรือ `TEXT_EXTRACTION_FAILED`

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

### จังหวะการ poll ที่แนะนำ

เริ่มที่ **5 วินาที** แล้ว backoff เป็นสองเท่าจนไม่เกิน 60 วินาที และหยุดเมื่อ `status` เป็นสถานะสุดท้าย
ถ้าส่งเป็น batch ให้ใช้ `GET .../knowledge-bases/{kb_id}/documents` (ไม่ส่ง `status`) เพื่อ poll
ครั้งเดียวต่อรอบแล้วกรองที่ client แทนการยิงรายเอกสาร เพราะทุก request ถูกหักจาก rate limit ของ
token ใบเดียวกัน

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
| 409 | `FILE_DUPLICATE` | ไฟล์เนื้อหาเดียวกันมีอยู่ใน Knowledge Base แล้ว | false |
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
"ส่งซ้ำ" ไม่ใช่ "ส่งผิด" — กรณีนี้ปลอดภัยที่จะข้ามไป ไม่ต้องส่งใหม่

เฉพาะ error ที่ `retryable: true` ควร retry ให้หน่วงแบบ exponential backoff อย่าวน retry ทันที

## Rate limit และ concurrency

แต่ละ token มีเพดานของตัวเอง ตั้งค่าได้ที่ **Advanced limits** ตอนสร้าง token
(ค่าเริ่มต้น 60 requests/min และ 5 concurrent requests) เพดานนี้**ใช้ร่วมกันระหว่าง Ingest API กับ `/mcp`**
เพราะเป็นงบประมาณของ token ใบเดียวกัน

การอัปโหลดใช้เวลานานกว่าการอ่าน จึงควรจำกัดจำนวน worker ที่อัปโหลดพร้อมกันไม่ให้เกินค่า concurrent
ที่ตั้งไว้ และใช้ batch endpoint เพื่อลดจำนวน request

## ชนิดไฟล์และขนาด

รองรับ `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.xls`, `.txt`, `.md`, `.html`, `.htm`, `.csv`, `.json`

MIME type ที่ส่งมาต้องตรงกับนามสกุล (ส่ง `application/octet-stream` ได้ ระบบจะเดาจากนามสกุลให้)
ขนาดสูงสุดกำหนดด้วย `MAX_FILE_SIZE_MB` (ค่าเริ่มต้น 100 MB)

PDF ที่เป็นภาพสแกนต้องมี OCR — ถ้าไม่ได้ตั้งค่าไว้ เอกสารจะขึ้น `status: failed` พร้อม
`error_code: OCR_REQUIRED` รายละเอียดดู [README](../README.md)

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
