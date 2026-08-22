# ข้อ 2 (E2E) — Findings ฉบับสมบูรณ์ (หลัง reprocess + วิเคราะห์ root cause)

## ผลสุดท้าย: 12/13 completed (legacy .xls = extractor bug จริง)

| กลุ่ม | อาการ | Root cause | รหัส |
|---|---|---|---|
| การ index ล้มเป็นชุด (6 ไฟล์) | RETRIEVAL_ENGINE_REJECTED | OpenRouter 402 in_flight_budget_exhausted — ingest ขนาน 13 ไฟล์ ล้น budget ของ account (e2e แชร์ account กับ prod); LightRAG ตอบ failed → ไม่ retry | F1 |
| error_code ค้างหลังสำเร็จ | csv completed แต่ error_code ยัง RETRIEVAL_ENGINE_REJECTED | ไม่เคลียร์ error_code ตอน completed | F2 |
| .xls (HTML-flavored) | TEXT_EXTRACTION_FAILED | extractor ไม่รองรับ HTML-in-xls (ไฟล์ .xls จากระบบราชการไทยมักเป็นแบบนี้) | F3 |
| EXTRACT_LEGAL_METADATA ค้าง queued | legal docs เสร็จแล้ว job ที่สองไม่เดิน | worker เดี่ยว + ลำดับ queue (ตรวจต่อว่า by-design หรือ starvation) | F4 |
| BUSY ผิดปกติ | rfc2616.txt วน RETRIEVAL_ENGINE_BUSY ทุกรอบ | **title มี `/` ("RFC 2616 HTTP/1.1") → LightRAG ตัด basename ที่ slash → source กลายเป็น `1.1`** — index สำเร็จแต่ find_document() หาไม่เจอ → recovery path ไม่ทำงาน → 409 ถูกตีเป็น BUSY ตลอด | F5 |
| คำตอบ insufficient_evidence กับเนื้อหาที่ index แล้ว | NIST CSF + กฎหมายไทย query ได้ 0 sources ทั้งที่ find_entities เห็น entities ของ doc เหล่านั้น | UNVERIFIED_VALIDITY (no confirmed effective date) กด weight (status_weight 0.35) จน evidence ตก threshold — เอกสาร legal/regulation ที่ extract วันที่ไม่ได้ถูกปฏิเสธทั้งที่มีเนื้อหา | F6 |
| zombie pipeline หลัง 402 | LightRAG pipeline busy ค้าง 7 docs | งานที่ตายกลางคันจาก 402 ค้าง PARSING/PROCESSING — resume อัตโนมัติเมื่อ credit กลับมา (พฤติกรรมดี) แต่ระหว่างนั้น ingest ใหม่ชน 409 | F7 |

## สิ่งที่พิสูจน์แล้ว (ผ่านจริง)
1. **OCR chain ไทย E2E**: PDF สแกน image-only 23MB → 1878 chars ไทยถูกต้อง (ผ่าน Softnix OCR)
2. **11 formats ผ่าน ingestion จริง**: pdf(digital)/pdf(scan)/txt/csv/xlsx/json/md/docx/pptx/html — เหลือ .xls (bug F3)
3. **MCP query E2E ทำงาน**: 9 tests → 7 PASS (EN retrieval ตรง, out-of-scope → insufficient_evidence, cross-KB leak ไม่มี)
4. **KB scoping แน่น**: token ของ gov-data ไม่เห็นเนื้อหา thai-law แม้ query ภาษาไทยตรง ๆ

## ข้อเสนอแนะการแก้ (ไปข้อ 3)
- F5: sanitize `/` ออกจาก source label ใน `_source_label()` (ฝั่ง retrieval.py) — แก้จุดเดียวจบ
- F1: RETRIEVAL_ENGINE_REJECTED จาก 402 ต้อง retryable + จำกัด concurrency index ต่อ worker
- F2: เคลียร์ error_code เมื่อ completed
- F6: เกณฑ์ validity ต้อง downgrade เป็น warning ไม่ใช่ hard-gate evidence
