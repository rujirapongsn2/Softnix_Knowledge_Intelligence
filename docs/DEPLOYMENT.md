# การติดตั้งและ Deploy

## Local / development

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
```

ตั้งค่าอย่างน้อย:

- `APP_SECRET_KEY`, `TOKEN_HASH_SECRET`, `INITIAL_ADMIN_PASSWORD` เป็นค่าที่สุ่มใหม่และไม่ commit
- `OPENROUTER_API_KEY` สำหรับ embedding, extraction และ answer generation
- `LIGHTRAG_API_KEY` เป็น credential ภายในของ LightRAG
- `WEB_PORT` (ค่าเริ่มต้น 8081), `API_PORT` (ค่าเริ่มต้น 8001)
- `EXT_OCR_KEY` หากต้องการ OCR PDF สแกน

บริการหลักคือ `web`, `api`, `worker`, `migrate`, `postgres`, `redis`, `neo4j` และ `lightrag` โดยข้อมูลอยู่ใน named volumes (`files`, `postgres`, `redis`, `neo4j`, `lightrag`)

## Production

ใช้ reverse proxy หรือ Cloudflare Tunnel ที่มี HTTPS:

```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --build
```

ไฟล์ production จะเปิด `APP_ENV=production`, `COOKIE_SECURE=true`, ปิด query text log และตั้ง restart policy ให้บริการหลัก ห้ามเปิดพอร์ต PostgreSQL, Redis, Neo4j หรือ LightRAG สู่สาธารณะ

## Migration และตรวจสอบ

`migrate` จะรัน `alembic upgrade head` ก่อน API เริ่มทำงาน หากรันแยก:

```bash
docker compose run --rm migrate
docker compose up -d --build api worker web
```

ตรวจสถานะด้วย:

```bash
curl http://localhost:8001/health
curl http://localhost:8001/ready
docker compose ps
```

`/ready` ต้องเห็น database, Redis, Neo4j, LightRAG และ external OCR เป็น `ready` ตามบริการที่ตั้งค่าไว้

## การเปลี่ยนแปลงที่มักต้องทำเพิ่ม

- เพิ่ม `MAX_FILE_SIZE_MB` แล้ว rebuild `web` และ `api` เพื่อให้ file picker, proxy และ API ใช้ค่าเดียวกัน (ค่าเริ่มต้น 100 MB)
- เปลี่ยน embedding model/dimension ต้อง reindex embeddings ทั้ง Knowledge Base อย่างควบคุม (ค่าเริ่มต้น dimension 1536)
- เพิ่ม/แก้ `published_at` ได้ตอน upload หรือผ่าน `PATCH /api/v1/documents/{id}/metadata`; ไม่มีการ backfill อัตโนมัติ
- หาก deploy API ใหม่แล้ว web ได้ `502` ให้ recreate web เพื่อให้ nginx resolve IP ของ API ใหม่: `docker compose up -d --force-recreate web`
