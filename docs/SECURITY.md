# ความปลอดภัย

- รหัสผ่านผู้ดูแล hash ด้วย Argon2id และใช้ HTTP-only session cookie
- MCP token เก็บเฉพาะ HMAC digest มี expiry, revoke, scope เครื่องมือ/Knowledge Base และ rate/concurrency limit ผ่าน Redis
- Upload ตรวจ extension/MIME, จำกัดขนาดแบบ streaming, กันไฟล์ซ้ำด้วย SHA-256 และเก็บไฟล์นอก web root
- เอกสารที่ผู้ใช้อัปโหลดถือเป็นข้อมูลไม่ปลอดภัย LightRAG/LLM จะได้รับ instruction boundary และ evidence ที่ถูกจำกัดขอบเขตเท่านั้น
- Production ต้องใช้ HTTPS, `COOKIE_SECURE=true`, ไม่เปิด data services สู่สาธารณะ และกำหนด CORS allowlist
- ห้าม commit `.env`, API key, secret หรือ token ดิบ

## Audit และ metrics

การกระทำของผู้ดูแลเก็บใน `audit_logs` โดยไม่เก็บ session secret หรือ token plaintext ส่วน `/metrics` เป็น Prometheus-compatible endpoint ควรเปิดผ่าน private network หรือ monitoring proxy เท่านั้น

Trace และ MCP activity เป็นข้อมูล redacted: ไม่เก็บ bearer token, authorization header, full prompt หรือ document body ค่า retention ของ trace/request/MCP และ audit แยกกันผ่าน `OBSERVABILITY_RETENTION_DAYS` และ `AUDIT_RETENTION_DAYS`
