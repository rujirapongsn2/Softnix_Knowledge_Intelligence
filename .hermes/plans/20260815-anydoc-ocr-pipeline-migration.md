# แผนเปลี่ยน Text-Extraction Pipeline ไปใช้ Anydoc-ocr Implementation Plan

> **For Hermes:** ใช้ plan นี้ task-by-task พร้อมทดสอบจริงทุกขั้น รายงาน PASS/FAIL ต่อ task

**Goal:** แทนที่ pipeline สกัดข้อความของ softnix-knowledge-intelligence (MarkItDown → pypdf → ExternalOcrClient) ด้วย Anydoc-ocr (~/Documents/code-mini/Anydoc-ocr) แบบครบวงจร พร้อมลบโค้ด/การตั้งค่าที่ทำงานซ้ำซ้อนออก

**Architecture:** anydoc (Rust wheel) เป็นตัวแปลงเอกสารหลัก + ตรวจหน้าสแกน/ฟอนต์ไทยพังในตัว แล้วส่งหน้าที่ต้อง OCR เข้า Python callback chain ที่เราเขียนเอง (Softnix → Mistral → Tesseract) ใช้ httpx ที่เป็น dependency อยู่แล้ว — ไม่ต้อง compile Rust features เพิ่ม ไม่ต้องมี curl ใน container

**Tech Stack:** anydoc-python 0.1.7 (abi3 wheel, สร้างใน Docker multi-stage ด้วย maturin), httpx, tesseract-ocr (tha+eng), poppler-utils (pdftoppm สำหรับ render หน้า — anydoc เรียกใช้), FastAPI/SQLAlchemy เดิม

---

## บริบทปัจจุบัน (สำรวจแล้ว 15 ส.ค. 2026)

### เส้นทางปัจจุบัน (`services.py`)

```text
extract_text(document)
 ├─ _markitdown_extract()          ← MarkItDown 0.1.6 (ช้า, Python)
 ├─ _repair_or_flag_pdf_text()     ← gate ไทยที่เพิ่งแก้ (Thai-ratio + codec recovery) ✅ เก็บไว้
 ├─ _legacy_extract_text()         ← pypdf/python-docx/BS4 fallback
 └─ OCR_REQUIRED → extract_scanned_pdf_with_external_ocr()   ← external_ocr.py (Softnix เท่านั้น)
```

### สิ่งที่ซ้ำซ้อนกับ Anydoc-ocr — ต้องถอด

| ส่วนในระบบปัจจุบัน | ทำหน้าที่เดียวกับใน Anydoc-ocr | การจัดการ |
|---|---|---|
| `_markitdown_extract()` | `anydoc.to_markdown()` (Rust, ~5ms vs MarkItDown ~1-3s) | **ลบ** — anydoc แทน |
| `_legacy_extract_text()` (PDF branch) | anydoc จัดการทุก format | **ลบ PDF branch** — เก็บ .txt/.html/.docx branch ไว้ใน phase แรก (anydoc ไม่รองรับ .html) |
| `external_ocr.py` ทั้งไฟล์ (107 บรรทัด) | `SoftnixOcr` engine ใน chain ใหม่ | **ลบทั้งไฟล์** — logic ย้ายเข้า `ocr_chain.py` |
| `EXT_OCR_*` settings 11 ตัว | `SOFTNIX_OCR_*` + `MISTRAL_API_KEY` + tesseract ในเครื่อง | **เปลี่ยนชื่อ/ลดจำนวน** (ดู Task 6) |
| stage `external_ocr_submit` ใน job progress | anydoc จัดการ render+OCR เอง | **เปลี่ยนชื่อ stage** เป็น `ocr_chain` |

### สิ่งที่ Anydoc-ocr ให้และระบบปัจจุบันไม่มี

1. Fast-path แปลงเอกสารเร็วกว่า ~100 เท่า
2. ตรวจ `pages_needing_ocr` แบบรายหน้า (ระบบปัจจุบัน OCR ทั้งไฟล์)
3. ตรวจฟอนต์ไทย map พัง (`encoding.rs` — จับ tone-mark pattern) เสริมกับ gate Thai-ratio ของเรา
4. **Mistral OCR เป็น fallback ชั้น 2** (ทดสอบ live แล้วใน Anydoc-ocr)
5. Tesseract ท้องถิ่นเป็นชั้นสุดท้าย (ระบบปัจจุบันไม่มีชั้นนี้เลย)

### ข้อจำกัดที่ตรวจแล้ว

- Container api/worker เป็น `python:3.12-slim` (Debian 13, linux/aarch64) — **ไม่มี curl/pdftoppm/tesseract**
- โฮสต์เป็น macOS arm64 → สร้าง wheel ต้อง build ใน container Linux (maturin ใน Dockerfile multi-stage)
- Docker registry อาจยังติด proxy — ใช้ base image `python:3.12-slim` ที่ pull แล้ว + `rust:slim` ถ้ามี cache มิฉะนั้นใช้ path `docker cp` ชั่วคราวเหมือนเดิม

---

## ลำดับ Task

### Task 1: สร้างโมดูล `ocr_chain.py` (Python callback chain)

**Objective:** สร้าง chain ทดแทน external_ocr.py — Softnix (httpx) → Mistral (httpx) → Tesseract (CLI)

**Files:**
- Create: `apps/api/app/ocr_chain.py`
- Test: `apps/api/tests/test_ocr_chain.py`

**โครงสร้าง:**

```python
"""OCR fallback chain for anydoc's to_markdown_with_ocr callback.

Order: Softnix OCR (best Thai) → Mistral OCR (Markdown quality)
→ Tesseract tha+eng (local, always available when installed).
First engine returning non-empty text wins; a failed engine is
skipped with a warning, matching the Rust FallbackOcr contract.
"""

class OcrChain:
    def __init__(self, settings): ...
    def recognize(self, image: bytes, page: int) -> str:
        # try each engine in order; raise RuntimeError("OCR_CHAIN_FAILED: ...") when all fail
```

- Engine 1 **Softnix**: ย้ายมาจาก `external_ocr.py` แบบ httpx (submit `/v3/ai-process-file` + `disable_structure=true` → poll `/status` → `/result` → `ai_processing.content` or `ocr_text`) — คง on_progress callback สำหรับ job progress
- Engine 2 **Mistral**: `POST https://api.mistral.ai/v1/ocr` แบบ base64 data URI → `pages[0].markdown` (พอร์ตจาก `mistral.rs` ที่ทดสอบ live แล้ว)
- Engine 3 **Tesseract**: `tesseract <tmp.png> stdout -l tha+eng --oem 1` ผ่าน subprocess พร้อม timeout (ต้องมี binary ใน container — Task 3)

**TDD steps:**
1. เขียน test 4 เคส: chain ตัวแรกสำเร็จ / ตัวแรกล้มไปตัวที่สอง / ทุกตัวล้ม raise / คืน "" ถือเป็นล้ม
2. รันให้ fail → implement → ผ่าน
3. Mock engines ด้วย httpx.MockTransport + fake subprocess

**Run:** `cd apps/api && ../../.venv/bin/python -m pytest tests/test_ocr_chain.py -q` → expect PASS

### Task 2: สร้างโมดูล `doc_extraction.py` (anydoc entry point)

**Objective:** ฟังก์ชันเดียว `extract_document_text(document) -> str` ที่ระบบเรียก

**Files:**
- Create: `apps/api/app/doc_extraction.py`
- Test: `apps/api/tests/test_doc_extraction.py`

```python
import anydoc  # firecrawl-anydoc / anydoc-python wheel

def extract_document_text(document, *, on_progress=None) -> str:
    """anydoc fast path → OCR chain for scanned/garbled pages →
    _repair_or_flag_pdf_text gate → raise OCR_REQUIRED-equivalent on total failure."""
    data = Path(document.storage_path).read_bytes()
    fmt = anydoc.format_from_path(document.storage_path)
    markdown = anydoc.to_markdown_with_ocr(data, fmt, ocr_chain.recognize)
    return gate(markdown)  # reuse _repair_or_flag_pdf_text from services.py
```

- Import anydoc แบบ try/except: ถ้าไม่มี wheel → raise `RuntimeError("ANYDOC_UNAVAILABLE")` ให้ caller เลือก legacy path (ช่วย test suite ที่ยังไม่มี wheel)
- ผูน `on_progress` เข้า chain (เพื่ออัปเดต `job.progress_percent`)

**TDD:** mock anydoc ด้วย monkeypatch; เคส: text PDF (ไม่เรียก OCR callback), scanned PDF (callback ถูกเรียก), anydoc หาย → ANYDOC_UNAVAILABLE

### Task 3: แก้ Dockerfile — system deps + สร้าง anydoc wheel

**Objective:** container มีทุกอย่างที่ chain ต้องใช้

**Files:**
- Modify: `apps/api/Dockerfile`

```dockerfile
FROM rust:1.96-slim AS anydoc-builder          # ถ้า pull ไม่ได้ ใช้ docker cp fallback
RUN cargo install maturin || pip install maturin
COPY --from=host anydoc-src /build/anydoc       # build context จาก ../Anydoc-ocr
WORKDIR /build/anydoc/python
RUN maturin build --release --out /wheels       # abi3-py310 linux/aarch64

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils tesseract-ocr tesseract-ocr-tha curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=anydoc-builder /wheels/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl . && rm /tmp/*.whl
```

**หมายเหตุ proxy:** ถ้า build ไม่ผ่านเพราะ registry — สำรองด้วยการ build wheel ใน container ชั่วคราว (`docker run --rm -v ~/Documents/code-mini/Anydoc-ocr:/src rust:slim`) แล้ว `docker cp` เข้าไปติดตั้งเหมือนที่เคยทำกับ retrieval.py

**Verify ใน container:** `python -c "import anydoc; print(anydoc.to_markdown_bytes(b'a,b\n1,2','csv'))"` + `tesseract --list-langs` เห็น `tha`

### Task 4: เปลี่ยนเส้นทางใน `services.py` + ลบโค้ดซ้ำ

**Objective:** เรียก anydoc ก่อน และถอดของเก่าออก

**Files:**
- Modify: `apps/api/app/services.py` (extract_text + process_next_job)
- Delete: `apps/api/app/external_ocr.py`
- Modify: `apps/api/pyproject.toml` (ถอด `markitdown[pdf,...]` + `pypdf` หลัง phase 2 ยืนยันแล้ว — phase แรกเก็บไว้เป็น fallback ระหว่าง transition)

**เส้นทางใหม่ใน `extract_text()`:**

```python
def extract_text(document: Document) -> str:
    ext = path.suffix.lower()
    if ext not in {".html", ".htm"}:          # anydoc ไม่รองรับ html
        try:
            return extract_document_text(document)   # anydoc + chain + gate
        except RuntimeError as exc:
            if str(exc) != "ANYDOC_UNAVAILABLE":
                raise                            # OCR_CHAIN_FAILED ฯลฯ จบที่นี่
    # fallback เดิม (MarkItDown/pypdf) — ถอดใน Task 8
    ...
```

**เปลี่ยนใน `process_next_job()`:**
- `extract_scanned_pdf_with_external_ocr(...)` → ลบ (chain อยู่ใน anydoc path แล้ว)
- stage `external_ocr_submit` → `ocr_chain`
- error code ชุด `EXTERNAL_OCR_*` → เพิ่ม `OCR_CHAIN_*` ใน TRANSIENT_PROCESSING_ERRORS mapping ที่ line ~2273

### Task 5: Settings migration

**Objective:** env vars ใหม่ + เลิกใช้ของเก่า

**Files:**
- Modify: `apps/api/app/config.py`
- Modify: `.env`, `.env.example`, `docker-compose.yml`

| เดิม | ใหม่ |
|---|---|
| `EXT_OCR_BASE_URL` | `SOFTNIX_OCR_BASE_URL` |
| `EXT_OCR_KEY` | `SOFTNIX_OCR_TOKEN` |
| `EXT_OCR_VERIFY_SSL` | `SOFTNIX_OCR_INSECURE_TLS` (กลับความหมาย) |
| `EXT_OCR_ENGINE`, `EXT_OCR_IMAGE_SIZE` | **ลบ** (engine กำหนดโดย chain) |
| `EXT_OCR_POLL_INTERVAL_SECONDS`, `EXT_OCR_PROCESSING_TIMEOUT_SECONDS`, `EXT_OCR_REQUEST_TIMEOUT_SECONDS` | รวมเป็น `SOFTNIX_OCR_TIMEOUT_SECONDS` |
| — | `MISTRAL_API_KEY` (ใหม่, optional) |
| — | `OCR_CHAIN_ENGINES=softnix,mistral,tesseract` (ลำดับปรับได้, optional) |

- เขียน compatibility alias ชั่วคราวใน config.py (อ่านเก่าถ้าใหม่ไม่มี) พร้อม log deprecation — ถอดใน Task 8

### Task 6: E2E ทดสอบไฟล์ทดสอบ 4 ชุดใน labtest01

**Objective:** พิสูจน์ระบบใหม่ด้วยไฟล์จริงทั้ง 4 ลักษณะ

| ไฟล์ | คาดหวัง |
|---|---|
| ฉบับที่ 8 (สแกนล้วน) | anydoc ตรวจ pages_needing_ocr → chain: Softnix สำเร็จ → ไทยอ่านได้ |
| ฉบับที่ 9 (mojibake) | anydoc garbled-detect + gate → OCR → ไทยอ่านได้ |
| ฉบับที่ 11 (สระเกาะผิด) | ตัดสินใจตาม gate — ยอมรับได้ทั้งเก็บ layer หรือ OCR แต่ต้องไม่ mojibake |
| ฉบับที่ 14 (text layer ดี) | **fast path** — ไม่เรียก OCR เลย, เร็วขึ้นชัดเจนเทียบ MarkItDown |

**Steps ต่อเอกสาร:** upload ผ่าน Ingest API (token `/tmp/ski-ingest-token`) → poll จน terminal → ตรวจ `extracted_text` เป็นไทย + query พร้อม citation → บันทึกเวลา extraction เทียบกับรอบก่อน (MarkItDown) เป็น benchmark

### Task 7: ทดสอบ fallback จริงของ chain

**Objective:** พิสูจน์ chain degrade ถูกต้องในระบบจริง

1. ปิด Softnix (token ผิด) → upload สแกน → ต้องข้ามไป Mistral → ไทยได้ (key ที่ให้ไว้ทดสอบแล้ว)
2. ปิด Mistral ด้วย → ต้องไป Tesseract ท้องถิ่น → ได้ข้อความ (ยอมรับคุณภาพต่ำกว่า)
3. คืนค่า → ปกติ

### Task 8: Cleanup ขั้นสุดท้าย

- ถอด MarkItDown/pypdf ออกจาก `pyproject.toml` (เมื่อ Task 6-7 ผ่านครบ)
- ถอด compatibility alias ของ env vars
- ลบ `_markitdown_extract`, `_legacy_extract_text` PDF branch
- อัปเดต `docs/INGEST_API.md` (ข้อผิดพลาด OCR ใหม่), `README.md` ส่วน OCR, `docs/ARCHITECTURE.md`
- รัน full suite + commit

---

## ไฟล์สรุป

| การกระทำ | ไฟล์ |
|---|---|
| สร้าง | `apps/api/app/ocr_chain.py`, `apps/api/app/doc_extraction.py`, `apps/api/tests/test_ocr_chain.py`, `apps/api/tests/test_doc_extraction.py` |
| แก้ | `apps/api/app/services.py`, `apps/api/app/config.py`, `apps/api/Dockerfile`, `apps/api/pyproject.toml`, `.env*`, `docker-compose.yml`, docs |
| ลบ | `apps/api/app/external_ocr.py` ทั้งไฟล์ |

## ความเสี่ยงและแนวทางรับมือ

### การตัดสินใจเรื่องลำดับ chain (บันทึกไว้เป็นหลัก)

เคยพิจารณา `Tesseract → Softnix → Mistral` (หน้าง่ายจบในเครื่องเร็ว/ฟรี) แต่**ตัดออก** เพราะ:

1. Tesseract ไทย "สำเร็จ" ด้วยข้อความคุณภาพต่ำเกือบเสมอ (แทรกช่องว่างทุกตัวอักษร) — first-non-empty-wins จะหยุดที่ Tesseract ตลอด ทำให้คุณภาพรวมตก
2. แก้ด้วย quality gate ได้ แต่เพิ่ม heuristic ที่ต้อง calibrate และพลาดแล้วขยะเข้า legal registry/embeddings ถาวร
3. Softnix เป็นเซิร์ฟเวอร์บริษัทเอง — ข้อได้เปรียบเรื่อง privacy/ต้นทุนของชั้นท้องถิ่นแทบไม่เหลือ
4. งานกฎหมายให้น้ำหนักความถูกต้องและ deterministic มากกว่าความเร็วของหน้าง่าย

สรุป: ใช้ `Softnix → Mistral → Tesseract` เป็นค่าเริ่มต้น + `OCR_CHAIN_ENGINES` ให้สลับลำดับผ่าน config ได้โดยไม่แก้โค้ด (เช่น environment ที่ต่อ Softnix ไม่ได้ อาจเลื่อน Tesseract ขึ้น first เป็นกรณีพิเศษ)

| ความเสี่ยง | แนวทาง |
|---|---|
| Build wheel linux/aarch64 ไม่ผ่าน (proxy/Rust toolchain) | Docker multi-stage; สำรอง build ใน container ชั่วคราวแล้ว docker cp |
| Gate ไทยเดิมชนกับ garbled-detect ของ anydoc | เก็บ gate ไว้หลัง anydoc — ทั้งสองตัว idempotent ซ้อนกันได้ (ทดสอบใน Task 6 เคสที่ 9/11) |
| เอกสารเก่าที่เคยผ่าน MarkItDown อาจให้ผลต่างเมื่อ reindex | ไม่ reindex อัตโนมัติ — เฉพาะเอกสารใหม่/reprocess |
| html ไม่อยู่ใน anydoc | เก็บ MarkItDown ไว้เฉพาะ html ใน phase แรก |
| Tesseract ชั่วคราวโหลดเครื่อง (chain ชั้น 3) | มี timeout ต่อหน้า + จำกัดเฉพาะเมื่อชั้น 1-2 ล้มเท่านั้น |

## เกณฑ์ผ่าน (Definition of Done)

1. Unit tests ทั้งชุดผ่าน (ชุดเดิม 206 + ใหม่ ~12)
2. E2E ไฟล์ทดสอบ 4 ชุด: ไทยอ่านได้ทุกไฟล์ + ฉบับที่ 14 ไม่แตะ OCR
3. Chain fallback พิสูจน์ได้ทั้ง 3 ระดับ
4. `external_ocr.py` หาย, MarkItDown เหลือเฉพาะ html path
5. Extraction เร็วขึ้นวัดได้จริง (ฉบับที่ 14 เทียบก่อน/หลัง)
