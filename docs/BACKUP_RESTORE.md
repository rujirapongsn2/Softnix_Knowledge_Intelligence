# สำรองข้อมูล กู้คืน และย้ายเครื่อง

ข้อมูลทั้งหมดของระบบอยู่ใน Docker volume 3 ตัว และไฟล์ `.env`:

| ส่วน | เก็บอะไร | วิธีสำรอง |
|---|---|---|
| PostgreSQL (`postgres`) | เอกสาร, chunk, vector, คิวงาน, metadata, legal registry, audit, trace และข้อมูลของ LightRAG (ตาราง `lightrag_*`) | `pg_dump` |
| Neo4j (`neo4j`) | graph ของระบบ และ graph ของ LightRAG (label `lightrag`) | `tar` ของ volume ขณะ Neo4j หยุด |
| ไฟล์ (`files`) | ไฟล์ต้นฉบับและไฟล์อ้างอิง | `tar` ของ volume |
| `.env` | secret ทั้งหมด | คัดลอก (สิทธิ์ 600) |

ไม่ต้องสำรอง Redis เพราะเก็บแค่ตัวนับ rate limit ของ token ส่วนคิวงานอยู่ใน PostgreSQL
volume `lightrag` เก็บแค่ storage แบบไฟล์จากก่อนย้ายไป Postgres ใช้สำหรับ rollback เท่านั้น จึงไม่ต้องสำรอง

## สำรองข้อมูล

```bash
bash deploy/backup.sh                     # เขียนไปที่ ~/softnix-backups/<วันที่-เวลา>-full
bash deploy/backup.sh /path/to/backup     # หรือระบุโฟลเดอร์เอง (ต้องว่าง)
```

script จะทำตามลำดับนี้:

1. ปฏิเสธการสำรองถ้ามีงานกำลังประมวลผลอยู่ ถ้าใส่ `--force` จะสำรองต่อ worker จะคืนงานเข้าคิวเมื่อถูกหยุด และข้อมูลที่สำรองจะเก็บงานเหล่านั้นไว้ในสถานะรอคิว
2. บันทึกจำนวนข้อมูลก่อนสำรอง ได้แก่ เอกสาร, chunk, เอกสารใน LightRAG, node ใน Neo4j และไฟล์
3. หยุด `worker`, `api` และ `lightrag` แล้ว dump PostgreSQL และตรวจว่าไฟล์ dump มีตาราง documents
4. หยุด `neo4j` แล้ว `tar` volume `neo4j` และ `files` จากนั้นตรวจว่าเปิดไฟล์ทั้งสองได้
5. คัดลอก `.env` เป็นไฟล์ชื่อ `env` และเขียน `manifest.json` ซึ่งเก็บจำนวนข้อมูล checksum (SHA-256) และ git commit
6. เปิดทุก service กลับมา แม้ขั้นใดขั้นหนึ่งจะล้ม

ผลวัดบนเครื่อง dev (2026-10-22):

- ข้อมูลมีเอกสาร 640 ฉบับ chunk 88,348 ชิ้น node ใน Neo4j 46,104 node และไฟล์ 1,586 ไฟล์
- service หยุด 3.5 นาที
- ขนาดรวม 3.5 GB: `postgres.dump` 1.1 GB, `neo4j.tgz` 0.2 GB และ `files.tgz` 2.3 GB

โฟลเดอร์ที่สำรองมี secret ทั้งหมด ต้องเก็บในที่ปลอดภัย และอย่าเก็บไว้บนเครื่องเดียวกับระบบเพียงที่เดียว

## กู้คืน

```bash
bash deploy/restore.sh BACKUP_DIR
```

script จะทำตามลำดับนี้:

1. ตรวจ checksum ทุกไฟล์กับ `manifest.json`
2. ถ้ายังไม่มี `.env` จะใช้ `.env` จากที่สำรองไว้
3. ถ้ามี `.env` อยู่แล้ว แต่ `APP_SECRET_KEY` หรือ `TOKEN_HASH_SECRET` ไม่ตรงกับที่สำรองไว้ จะปฏิเสธ เพราะ session และ API token ทุกใบจะใช้ไม่ได้
4. ถ้า project นั้นมีเอกสารอยู่แล้ว จะปฏิเสธ ถ้าใส่ `--force` จะลบข้อมูลเดิมแล้วกู้คืนทับ
5. กู้คืน PostgreSQL แล้ววางข้อมูลกลับลง volume `neo4j` และ `files`
6. เปิดทุก service ระบบจะรัน migration ให้ข้อมูลตรงกับ code จากนั้นรอจน `/ready` ตอบ
7. เทียบจำนวนข้อมูลกับ `manifest.json` ถ้าไม่ตรงกัน จะจบด้วย exit code ที่ไม่ใช่ 0

`--no-worker` จะเปิดระบบโดยไม่เปิด worker เพื่อไม่ให้งานที่ค้างในคิวเริ่มทำงาน และเสียค่า LLM

### ซ้อมกู้คืนบนเครื่องเดียวกัน

กู้คืนลง compose project แยก โดยใช้พอร์ตอื่น ระบบที่ใช้งานอยู่จะไม่ถูกแตะต้อง:

```bash
COMPOSE_PROJECT_NAME=skrestore API_PORT=8101 WEB_PORT=8181 bash deploy/restore.sh BACKUP_DIR --no-worker
# ตรวจเสร็จแล้วลบ project ซ้อม (ลบเฉพาะ volume และ image ของ skrestore)
COMPOSE_PROJECT_NAME=skrestore docker compose down -v --rmi local
```

ผลการซ้อมบนเครื่อง dev (2026-10-22) ใช้เวลา 4 นาที รวมการ build image:

- จำนวนข้อมูลทั้ง 5 รายการตรงกับที่สำรองไว้
- `/ready` ตอบ ready ทุก dependency และเข้าระบบด้วยรหัสผ่านเดิมได้
- ไฟล์อ้างอิงที่ดาวน์โหลดมี SHA-256 ตรงกับต้นฉบับ
- ลำดับผลค้นหาต่างจากระบบเดิมเล็กน้อย เพราะ `pg_restore` สร้าง HNSW vector index ใหม่ และ index แบบนี้หาเพื่อนบ้านแบบประมาณ เมื่อปิด index แล้วค้นแบบตรงทุกแถว ผลของทั้งสองระบบตรงกันทุกอันดับ ข้อมูลจึงเหมือนเดิม

## ย้ายเครื่อง

1. ติดตั้งเครื่องใหม่ตามขั้น 1–2 ของ [README](../README.md#การติดตั้ง):
   - clone repository และ `Anydoc-ocr`
   - ติดตั้ง embedding server ด้วยโมเดลเดิม (`embeddinggemma-2`, 768 มิติ) ถ้าเปลี่ยนโมเดล ต้องสร้าง vector ใหม่ทั้งหมด และ index LightRAG ใหม่ ซึ่งเสียค่า LLM
   - ติดตั้ง reranker ถ้าใช้อยู่
2. ที่เครื่องเดิม รัน `bash deploy/backup.sh` แล้วคัดลอกโฟลเดอร์ที่สำรองไปเครื่องใหม่ เช่นด้วย `rsync` หรือ disk ภายนอก
3. ที่เครื่องใหม่ ยังไม่ต้องสร้าง `.env` แล้วรัน `bash deploy/restore.sh BACKUP_DIR`
4. แก้ค่าใน `.env` ที่ขึ้นกับเครื่อง เช่น `EMBEDDING_BASE_URL` และ `RERANK_BASE_URL` ถ้าที่อยู่ของ server เปลี่ยน แล้วรัน `docker compose up -d`
5. รัน `bash deploy/check_services.sh` แล้วตรวจต่อดังนี้:
   - ค้นหาแล้วได้ citation
   - ดาวน์โหลดไฟล์อ้างอิงได้
   - client เดิมใช้ API token เดิมได้
6. หยุดระบบที่เครื่องเดิม เพื่อไม่ให้ client ส่งเอกสารเข้าสองที่

ระหว่างขั้นที่ 2 ถึงขั้นที่ 6 ให้หยุดการส่งเอกสารจาก client เอกสารที่ส่งเข้าเครื่องเดิมหลังสำรองไปแล้วจะไม่ถูกย้าย
