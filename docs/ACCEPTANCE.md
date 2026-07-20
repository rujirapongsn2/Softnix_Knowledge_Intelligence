# Acceptance walkthrough

## เส้นทางพื้นฐาน

ใช้ fixture [`fixtures/app-01-architecture.txt`](../fixtures/app-01-architecture.txt)

1. `docker compose up --build` แล้วตรวจ `/ready`
2. Login, สร้างและ activate Knowledge Base
3. Upload fixture และรอ job เป็น `completed`
4. Query `APP-01` แล้วตรวจ citation
5. สร้าง token แบบจำกัด KB/tool, เรียก MCP `search_knowledge` แล้วทดสอบ disable/revoke
6. สำหรับคำถาม “มีกี่เอกสาร/แบ่งเป็นประเภทใด” เรียก `document_inventory_summary` และตรวจ `total_documents`, `groups`, `document_registry` trace; หาก token เดิมไม่มี tool ให้ส่งคำถามต้นฉบับผ่าน `search_knowledge` เพื่อทดสอบ deterministic fallback

## Auto Retrieval Strategy

ชุด contract test อยู่ที่ `apps/api/tests/test_planner.py` ครอบคลุม 7 รูปแบบ: VPN/how-to, entity relationship, impact, ปัจจัยความล่าช้า, ข่าวตามเดือน/ปี, เลขเอกสาร และภาพรวมกราฟ โดยตรวจ intent, channels, graph scope/depth, entity/document ID และ date range

ชุด service/API test อยู่ที่ `apps/api/tests/test_api.py` ตรวจ plan และ trace พร้อมหลักฐานจริง รวม exact lookup, date filter, graph local/global, impact traversal และ `enable_reranker=false`

รัน:

```bash
cd apps/api
pytest -q
```

## Legal registry

Fixture กฎหมายอยู่ที่ [`fixtures/legal/`](../fixtures/legal/) และ acceptance test อัตโนมัติอยู่ใน `apps/api/tests/test_legal_acceptance.py` ครอบคลุม upload → legal extraction → review relationship → resolve version → query ตาม `as_of_date`

สิ่งที่ต้องยืนยันคือฉบับที่ถูกต้องมี citation, ฉบับ repealed/superseded ถูกกรองหรือมี warning, ความสัมพันธ์ที่ยังไม่ review ไม่มีผล และ KB ที่ไม่มีเอกสารกฎหมายยังทำงานเหมือนเดิม
