# การติดตั้งและ Deploy

## Local / development

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
```

ตั้งค่าอย่างน้อย:

- `APP_SECRET_KEY`, `TOKEN_HASH_SECRET`, `INITIAL_ADMIN_PASSWORD` เป็นค่าที่สุ่มใหม่และไม่ commit
- `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` ใช้ provider ใดก็ได้ที่รองรับ OpenAI API (ค่าเริ่มต้น OpenRouter กับ `anthropic/claude-sonnet-5.5`) `LLM_MODEL` ใช้กับการสกัด metadata และข้อมูลกฎหมาย การแนะนำความสัมพันธ์ทางกฎหมาย การวางแผน retrieval และ LightRAG graph extraction (เมื่อไม่ได้ตั้ง `LIGHTRAG_LLM_MODEL`) ส่วนคำตอบใช้ `LLM_ANSWER_MODEL` (ค่าว่างใช้ `LLM_MODEL`) ดู `.env.example` สำหรับ `LLM_API_DIALECT`, `LLM_TEMPERATURE` และ `RERANK_*` ชื่อเดิม `OPENROUTER_*` ยังอ่านได้
- เมื่อเครดิตของผู้ให้บริการ AI หมดหรือ key ใช้ไม่ได้ (402, 401) งานที่ต้องใช้ผู้ให้บริการนั้นจะรอในคิวโดยไม่เสียจำนวนครั้งที่ลอง worker ตรวจผู้ให้บริการทุก 1 ถึง 10 นาที และงานจะทำต่อเองเมื่อผู้ให้บริการตอบได้อีกครั้ง ไม่ต้องกดประมวลผลใหม่ ดูสถานะที่ `services.provider_holds` ของ `GET /api/v1/system/dashboard`
- `EMBEDDING_BASE_URL` (+ `EMBEDDING_API_KEY` ถ้า server ตั้ง key) ชี้ไปที่ embedding server แบบ OpenAI-compatible ดู [deploy/embedding-server](../deploy/embedding-server/README.md)
- `LIGHTRAG_API_KEY` เป็น credential ภายในของ LightRAG
- `WEB_PORT` (ค่าเริ่มต้น 8081), `API_PORT` (ค่าเริ่มต้น 8001)
- `EXT_OCR_KEY` หากต้องการ OCR PDF สแกน

บริการหลักคือ `web`, `api`, `worker` (3 replicas ปรับด้วย `WORKER_REPLICAS`), `migrate`, `postgres`, `redis`, `neo4j` และ `lightrag` โดยข้อมูลอยู่ใน named volumes (`files`, `postgres`, `redis`, `neo4j`, `lightrag`)
คิวงานอยู่ใน PostgreSQL ส่วน Redis เก็บตัวนับ rate limit ของ token และ LightRAG เก็บข้อมูลใน PostgreSQL และ Neo4j

## Production

ใช้ reverse proxy หรือ Cloudflare Tunnel ที่มี HTTPS:

```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --build
```

ไฟล์ production จะเปิด `APP_ENV=production`, `COOKIE_SECURE=true`, ปิด query text log และตั้ง restart policy ให้บริการหลัก ห้ามเปิดพอร์ต PostgreSQL, Redis, Neo4j หรือ LightRAG สู่สาธารณะ

**Swagger UI เปิดอยู่โดยค่าเริ่มต้น** — FastAPI ให้ `/docs` และ `/openapi.json` มาเสมอและระบบไม่ได้ปิดไว้
ใช้เป็น schema reference ของ endpoint ทั้งหมดรวมถึง [Ingestion API](INGEST_API.md) ได้สะดวกตอนพัฒนา
แต่บน production ควร block ทั้งสอง path ที่ reverse proxy เพราะเปิดเผยโครงสร้าง API ทั้งระบบให้คนนอก
(ตัว endpoint ยังต้องยืนยันตัวตนตามปกติ การเปิด `/docs` ไม่ได้ให้สิทธิ์เข้าถึงข้อมูล)

## Migration และตรวจสอบ

`migrate` จะรัน `alembic upgrade head` ก่อน API เริ่มทำงาน หากรันแยก:

```bash
docker compose run --rm migrate
docker compose up -d --build api worker web
```

ตรวจสถานะด้วย:

```bash
bash deploy/check_services.sh
```

script ตรวจว่า container ทุกตัวรันอยู่ และ `migrate` จบด้วย exit code 0 ตรวจ `/health`, `/ready` และ Web UI แล้วเรียก LLM, embedding server และ reranker (ถ้าเปิดไว้) จากใน container ของ api
ความหมายของแต่ละข้อดูที่ [README](../README.md#5-ตรวจสถานะหลังติดตั้ง)

`/ready` ต้องเห็น database, redis, neo4j, lightrag และ embeddings เป็น `ready` ส่วน ocr_chain จะตรวจเฉพาะเมื่อตั้ง `SOFTNIX_OCR_TOKEN` แล้ว ถ้า Softnix OCR ตอบไม่ได้แต่ใน `OCR_CHAIN_ENGINES` ยังมี engine ที่อ่านเอกสารสแกนได้ (Mistral ที่ตั้ง key ไว้ หรือ Tesseract ที่ติดตั้งใน image) `/ready` จะรายงาน ocr_chain เป็น `degraded` และยังตอบ 200 ถ้าไม่เหลือ engine สำรอง จึงจะตอบ 503
ถ้า `SOFTNIX_OCR_*` ยังเป็นค่าตัวอย่างจาก `.env.example` `/ready` จะตอบ 503 และ api จะไม่ขึ้นเป็น healthy

## การเปลี่ยนแปลงที่มักต้องทำเพิ่ม

- เพิ่ม `MAX_FILE_SIZE_MB` แล้ว rebuild `web` และ `api` เพื่อให้ file picker, proxy และ API ใช้ค่าเดียวกัน (ค่าเริ่มต้น 100 MB)
- เปลี่ยน embedding model/dimension (ค่าเริ่มต้น `embeddinggemma-2`, 768 มิติ) ต้องรัน migration ปรับคอลัมน์ vector, reindex embeddings ทุก Knowledge Base และ ingest LightRAG ใหม่ ดูขั้นตอนใน [deploy/embedding-server](../deploy/embedding-server/README.md)
- เพิ่ม/แก้ `published_at` ได้ตอน upload หรือผ่าน `PATCH /api/v1/documents/{id}/metadata`; ไม่มีการ backfill อัตโนมัติ
- nginx ฝั่ง web resolve ชื่อ service `api` ซ้ำผ่าน Docker DNS จึงรองรับการ recreate API container โดยไม่ต้อง restart web ตาม
- migration `0023_token_ingest_scope` ถอน wildcard ของ `allowed_tools` (รายการว่างเคยหมายถึง "ทุก tool") โดยเขียนชื่อ tool ทั้ง 9 ตัวลงในแถวเดิมที่ค่าว่าง สิทธิ์ของ token เก่าจึงเท่าเดิมทุกใบ และ `allowed_scopes` เริ่มต้นเป็นรายการว่าง = ยังไม่มีใบใดเขียนได้ ต้องเปิด "Write access" ให้ใบใหม่เองเมื่อจะใช้ Ingestion API
