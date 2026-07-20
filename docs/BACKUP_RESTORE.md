# Backup และ Restore

ควรสำรองข้อมูลต่อไปนี้พร้อมกัน:

- PostgreSQL volume: metadata, chunks, vectors, jobs, legal registry, audit และ traces
- Neo4j volume: graph projection
- `files` volume: ไฟล์ต้นฉบับ
- Redis volume หากต้องการรักษาคิวงานที่ยังไม่เสร็จ
- `.env` และไฟล์ deployment ที่ป้องกันการเข้าถึง (เก็บ secret อย่างปลอดภัย)

## ลำดับ restore

1. หยุด API/worker เพื่อไม่ให้เขียนข้อมูลระหว่างกู้คืน
2. กู้ PostgreSQL และ Neo4j ก่อน แล้วกู้ `files` กลับ path เดิม
3. เริ่มบริการด้วย migration ที่ตรงกับ source code (`alembic upgrade head`)
4. ตรวจ `/health`, `/ready`, จำนวนเอกสาร และ query ที่มี citation อย่างน้อยหนึ่งรายการ
5. ตรวจ graph projection และ reindex embeddings เฉพาะเอกสารที่ขาด vector หากจำเป็น
