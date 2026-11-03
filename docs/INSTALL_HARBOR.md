# ติดตั้งจาก Harbor Registry / Install from Harbor

> ฉบับย่อทีละขั้นตอน: [INSTALL_STEP_BY_STEP.md](INSTALL_STEP_BY_STEP.md)

ติดตั้ง Softnix Knowledge Intelligence จาก image สำเร็จรูปบน `registry.softnix.co.th/ski` ไม่ต้องมี source code และไม่ต้อง build
Install from prebuilt images in `registry.softnix.co.th/ski`. No source checkout or build is needed.

## Images

| Image | ที่มา / Source | Platforms |
|---|---|---|
| `ski/api:<version>` | `apps/api/Dockerfile` (api, worker, migrate ใช้ image เดียวกัน) | linux/amd64, linux/arm64 |
| `ski/web:<version>` | `apps/web/Dockerfile` | linux/amd64, linux/arm64 |
| `ski/pgvector:mirror-…`, `ski/redis:mirror-…`, `ski/neo4j:mirror-…`, `ski/lightrag:mirror-…` | สำเนา public image ที่ pin digest ไว้ใน `docker-compose.yml` / byte-for-byte mirrors | amd64, arm64 |

Tag ของ release: `v1.2.3`, `1.2` และ `sha-<commit>` สร้างโดย `.github/workflows/release.yml` เมื่อ push tag `v*`

## สิ่งที่ต้องมี / Prerequisites

- Linux server (amd64 หรือ arm64), Docker Engine 24+ และ Docker Compose v2, RAM 8 GB ขึ้นไป, disk 40 GB ขึ้นไป
- บัญชี pull ของ Harbor (robot account ของ project `ski` เช่น `robot$ski+customer-pull`) หรือ bundle แบบ offline
- บริการที่ติดตั้งแยก (stack นี้ไม่มีให้) / installed separately:
  - **LLM**: server แบบ OpenAI-compatible (`/v1/chat/completions`) เช่น vLLM, Ollama, LiteLLM หรือ OpenRouter
  - **Embedding**: OpenAI-compatible `/v1/embeddings` เช่น llama-server + `embeddinggemma-2` (768 มิติ) ดู [deploy/embedding-server](../deploy/embedding-server/README.md)
  - **Reranker** (ไม่บังคับ / optional): ดู [deploy/reranker-server](../deploy/reranker-server/README.md)
- HTTPS reverse proxy (nginx, Caddy, Cloudflare Tunnel) หน้า `web` สำหรับใช้งานจริง

## ติดตั้ง / Install

ไฟล์ที่ต้องมีในเครื่อง: `docker-compose.release.yml`, `.env.release.example`, `deploy/` (ได้จาก release bundle หรือ repo)

```bash
bash deploy/install.sh
```

script จะ / the script:

1. สร้าง `.env` จาก `.env.release.example` (mode 600) และสุ่ม `APP_SECRET_KEY`, `TOKEN_HASH_SECRET`, `POSTGRES_PASSWORD`, `NEO4J_PASSWORD`, `LIGHTRAG_API_KEY`, `INITIAL_ADMIN_PASSWORD`
2. ให้แก้ `.env`: `LLM_*`, `EMBEDDING_*`, `RERANK_*`, `LLM_APP_URL`, `COOKIE_SECURE`
3. `docker login registry.softnix.co.th` (ใช้ `HARBOR_USERNAME`/`HARBOR_PASSWORD` จาก environment ถ้ามี ไม่มีจะถาม)
4. `docker compose pull` แล้ว `up -d` (`migrate` รัน `alembic upgrade head` ก่อน api เสมอ)
5. รัน `deploy/check_services.sh` และแสดงรหัส admin ครั้งแรกครั้งเดียว

`.env` ตั้ง `COMPOSE_FILE=docker-compose.release.yml` ไว้ คำสั่ง `docker compose ...`, `deploy/backup.sh` และ `deploy/restore.sh` จึงใช้ไฟล์ release เอง

## ตั้งค่า LLM / LLM settings

ระบบเรียก LLM ผ่าน OpenAI-compatible API อย่างเดียว เลือกได้สองแบบ / two supported setups:

**On-prem (ค่าแนะนำสำหรับลูกค้า / default for customers)**

```ini
LLM_BASE_URL=http://<llm-host>:<port>/v1
LLM_API_KEY=none            # server ที่ไม่มี auth ก็ต้องใส่ค่าอะไรก็ได้ ค่าว่าง = ปิด LLM
LLM_MODEL=<ชื่อ model ตามที่ /v1/models แสดง>
LLM_API_DIALECT=compatible
LLM_REASONING_EFFORT=       # ว่าง เว้นแต่ model รองรับ reasoning_effort
```

**OpenRouter**

```ini
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-...
LLM_MODEL=anthropic/claude-sonnet-5.5
LLM_API_DIALECT=            # ว่าง = เลือก openrouter เองจาก URL
LLM_REASONING_EFFORT=low
```

LightRAG ใช้ค่า `LLM_*` ชุดเดียวกัน ตั้ง `LIGHTRAG_LLM_MODEL` หรือ `LLM_ANSWER_MODEL` ได้ถ้าต้องการ model แยก
`deploy/check_services.sh` ตรวจว่า provider ตอบได้และมี model ที่ตั้งไว้

Embedding และ reranker ต้องเป็น address ที่ container เข้าถึงได้ ห้ามใช้ `localhost`
บน Linux `host.docker.internal` ใช้ไม่ได้ ถ้าไม่ได้เพิ่ม `extra_hosts` ให้ใช้ IP หรือชื่อเครื่องแทน

## OCR และ `/ready`

- Tesseract (ไทย + อังกฤษ) อยู่ใน image api แล้ว ใช้ได้ทันที `OCR_CHAIN_ENGINES=tesseract` เป็นค่าเริ่มต้นของ release
- Softnix OCR: ตั้ง `SOFTNIX_OCR_BASE_URL` + `SOFTNIX_OCR_TOKEN` และ `OCR_CHAIN_ENGINES=softnix,mistral,tesseract`
- `/ready` ตรวจ Softnix OCR **เฉพาะเมื่อตั้งทั้ง URL และ token** ถ้าเว้นว่างจะไม่ตรวจ
  ถ้าใส่ค่าตัวอย่างหรือ server ตอบไม่ได้ `/ready` จะรายงาน `ocr_chain: degraded` (ยังตอบ 200 เพราะมี Tesseract/Mistral สำรอง)
  และตอบ 503 เฉพาะเมื่อไม่มี engine สำรองใน `OCR_CHAIN_ENGINES` ซึ่งทำให้ api ไม่ healthy
  / `/ready` probes Softnix OCR only when both values are set; a failing probe is `degraded` while a fallback engine remains, and 503 only without one.

## อัปเกรด / Upgrade

```bash
bash deploy/backup.sh                     # แนะนำก่อนอัปเกรดทุกครั้ง
bash deploy/install.sh --version v1.3.0   # ตั้ง SKI_VERSION, pull, migrate, restart
```

ย้อนกลับ: ตั้ง `SKI_VERSION` เดิมแล้ว `docker compose up -d` (ถ้า migration เปลี่ยน schema ให้ restore จาก backup)

## ติดตั้งแบบ offline / Air-gapped

บนเครื่องที่เข้าถึง Harbor ได้ / on a connected machine:

```bash
bash deploy/offline-bundle.sh save v1.0.0 linux/amd64   # -> dist/ski-v1.0.0-linux-amd64.tar.gz (~3.5-4 GB)
```

บนเครื่องลูกค้า / on the customer's server:

```bash
tar xzf ski-v1.0.0-linux-amd64.tar.gz && cd ski-v1.0.0-linux-amd64
bash deploy/offline-bundle.sh load
bash deploy/install.sh --offline
```

bundle ไม่มี model ของ embedding/reranker/LLM ต้องเตรียมแยก

## ค่าที่ควรรู้ / Notes

- `MAX_FILE_SIZE_MB` อ่านตอน container เริ่ม (web เขียน `/config.js` และ nginx `client_max_body_size`) แก้ `.env` แล้ว `docker compose up -d web api` ไม่ต้อง build ใหม่
- `POSTGRES_*` และ `NEO4J_PASSWORD` ใช้ตอนสร้าง volume ครั้งแรกเท่านั้น เปลี่ยนภายหลังต้อง `ALTER USER` / `neo4j-admin` ด้วย
- api เปิดที่ `127.0.0.1:${API_PORT}` เท่านั้น ผู้ใช้เข้าผ่าน web (`WEB_PORT`) ห้ามเปิดพอร์ต Postgres, Redis, Neo4j, LightRAG สู่ภายนอก
- block `/docs` และ `/openapi.json` ที่ reverse proxy บน production ([DEPLOYMENT.md](DEPLOYMENT.md))

## สำหรับผู้ดูแล release / For maintainers

Harbor:

- project `ski` (private) เปิด vulnerability scan และ tag immutability สำหรับ `v*` และ `mirror-*`
- robot account `robot$ski+ci-push`: push + pull (ใช้ใน GitHub Actions)
- robot account `robot$ski+customer-pull`: pull อย่างเดียว (ให้ผู้ติดตั้ง/ลูกค้า แยกต่อลูกค้าได้ เพื่อเพิกถอนรายคน)

GitHub Actions secrets: `HARBOR_USERNAME`, `HARBOR_PASSWORD` (robot `robot$ski+ci-push`) และ `ANYDOC_TOKEN` ถ้า repo Anydoc-ocr เป็น private
Variable (optional): `ANYDOC_REF` เปลี่ยน commit ของ Anydoc-ocr ที่ใช้ build

ออก release: `git tag v1.0.0 && git push origin v1.0.0` หรือกด Run workflow ที่ Actions > release
