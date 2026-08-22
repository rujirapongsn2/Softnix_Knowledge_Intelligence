# SKI Acceptance Report — ข้อ 1-6 สรุปรวม

**ระบบ**: Softnix Knowledge Intelligence (knowledge.softnix.ai)
**Repo**: softnix-knowledge-intelligence @ main
**วันที่**: 2026-08-22
**สถานะรวม**: ✅ **PASS** (ทุกข้อ verify ด้วยการรันจริง)

---

## ข้อ 1 — ปิดงานค้างให้ main = prod ✅ PASS
- Findings 7 ข้อจาก review แรก แก้ครบ (main.jsx/main.py/translations 897=897 keys)
- Suite: 253 passed → deploy prod → healthy
- Commit: `bec5550`, `0ab4cfc` + รอบสอง fix(ingest-samples,i18n,ops)

## ข้อ 2 — E2E จริงบน ski-e2e ✅ PASS (สรุปใหม่ 14/14)
- Corpus จริงจาก internet 11 formats, 6 KB + 6 ingest tokens
- 13 queued → 5 completed / 8 failed(402) → หลัง fix env caps → **14/14 completed**
- พบ F1-F8 findings สำหรับข้อ 3-5

## ข้อ 3 — แก้ bugs F1-F5, F7-F8 ✅ PASS
- full-stack แก้ครบ + code-review รอบแรก FAIL (M1 semaphore no-op, M2 error_code) → แก้ compose env caps (402 fix ที่ถูกจุด)
- tests: 276 passed; e2e 14/14 completed, .xls 13,359 chars, EXTRACT_LEGAL_METADATA 4/4
- Commit: `8116aae`

## ข้อ 4 — Cross-KB dedup + delete cascade + F8 ✅ PASS
- 4a: text เดียวกัน 2 KB → both completed (เดิมตาย RETRIEVAL_ENGINE_REJECTED)
- 4b: DELETE endpoint → PURGE_REMOTE_INDEX job → ghost หายจาก LightRAG KV จริง; re-upload ผ่าน
- 4c: migration 0030 partial unique `uq_document_checksum_live` + IntegrityError handler unique-only; fresh-install proof (pgvector เปล่า chain 0025→0030 ผ่าน)
- review C1/M1 แก้ครบ; 277 passed
- Commit: `830b41d`; prod deploy + migration applied + **backup `ski-prod-backup-20260822-1052.dump`**

## ข้อ 5 — MCP query E2E + F6 ✅ PASS
- F6 option (a): LLM ไม่ cite → ทิ้ง answer คง sources + fallback "พบหลักฐาน..." (M1: INVALID กรองเหลือ valid_ids; info#1: prefix เฉพาะมี answer)
- **Bug ใหม่ที่จับได้**: LLM planner ตัด vector กับ query ไทยเต็มประโยค → Postgres FTS (ไม่มี Thai tokenizer) ได้ 0 sources → guard thai-ratio ≥0.2 บังคับ vector + tests 2
- MCP E2E **18/18**: 9 tools, TH+EN, F6, cross-KB isolation, rate limit โดนพอดี call 61
- Commit: `b887d80`; prod deploy healthy

## ข้อ 6 — Production hygiene + UxUI + acceptance ✅ PASS
### 6a UxUI Content (uxui-content-writer + PM verify)
- Audit browser จริงทุก view ทั้ง 2 ภาษา → พบ 56 TH keys อังกฤษล้วน/อ่านเปะปะ
- แก้ 35 keys (ไทยเต็ม/ผสมเทคนิคในวงเล็บ) — proper nouns เก็บอังกฤษตามหลักการ
- Build ผ่าน + browser verify TH 9/9 PASS, EN ไม่แตะ (spot-check 4/4)
### 6b Test cases สำหรับ user
- `docs/MANUAL_TEST_CASES.md` — 6 KB × 3-5 เคส + ระบบ (rate limit, F6, i18n, ingest, MCP) = **28 เคส** พร้อม expected results
### 6c Production hygiene
- Backup ก่อนแตะ: `ski-prod-backup-20260822-1228.dump` (51.8MB)
- **Purge ghost 57 ตัว** (soft-deleted ก่อนมี cascade) — enqueue PURGE jobs ผ่าน job machinery เดียวกับ endpoint (ไม่ hard-delete) → drain ครบ
- ตรวจ: stuck jobs 0, zombie running 0, tokens 69 (จะ clean ตาม policy user)
### 6d Deploy prod commit ใหม่
- translations.js + MANUAL_TEST_CASES.md → build → deploy → verify

---

## สถิติสุดท้าย
- **Tests**: 279 passed, 1 skipped / ruff clean
- **Commits รอบนี้**: `8116aae` → `830b41d` → `b887d80` → (6) UxUI commit
- **E2E**: 14/14 ingest, 18/18 MCP, cross-KB, F6, rate limit
- **Prod**: healthy, migration 0030, ghost purge 57/57, backup 2 ชุด (1052, 1228)

## Known limitations (ซื่อตรง)
- analyze_impact บน corpus E2E คืนผลว่าง (legal_instrument_relations=0 — โครงสร้างข้อมูลไม่มี relations ในตัว corpus ทดสอบ ไม่ใช่ bug)
- rate limit ส่งเป็น JSON-RPC error บน HTTP 200 (สไตล์ MCP transport; client ต้องอ่าน body)
- LightRAG KV ghosts จาก probe docs บางตัว busy-lock ตอนลบ — drain เอง ไม่กระทบผล
- e2e แชร์ OpenRouter กับ prod — concurrency จำกัดระหว่าง E2E ตลอด
