# ติดตั้ง SKI จาก Harbor แบบทีละขั้นตอน

คู่มือฉบับย่อสำหรับผู้ติดตั้ง ทำตามทีละข้อได้เลย รายละเอียดเต็มดูที่ [INSTALL_HARBOR.md](INSTALL_HARBOR.md)

## 0. สิ่งที่ต้องเตรียมก่อน

- เครื่อง Linux แบบ amd64 หรือ arm64 ที่มี RAM 8 GB ขึ้นไป และ disk 40 GB ขึ้นไป
- ติดตั้ง Docker 24 ขึ้นไป และ Docker Compose v2
- LLM ที่เรียกผ่าน API แบบ OpenAI ได้ เช่น vLLM, Ollama หรือ OpenRouter
- Embedding server ที่เรียกผ่าน API แบบ OpenAI ได้ เช่น llama-server ที่รัน `embeddinggemma-2` (768 มิติ)
- Reranker จะมีหรือไม่มีก็ได้
- Harbor pull credentials ที่ได้รับจาก Softnix: username ของ robot account (รูปแบบ `robot$ski+customer-pull`) และ token

## 1. ดาวน์โหลดไฟล์ติดตั้ง

```bash
git clone https://github.com/rujirapongsn2/Softnix_Knowledge_Intelligence.git ski
cd ski
```

## 2. รันตัวติดตั้งรอบแรก

```bash
bash deploy/install.sh
```

script จะสร้างไฟล์ `.env` และสุ่มรหัสผ่านต่างๆ ให้เอง แล้วหยุดรอให้แก้ `.env` ก่อน ระหว่างนี้ให้เปิดอีกหน้าต่างหนึ่งไว้ทำข้อ 3

## 3. แก้ไฟล์ `.env` เฉพาะค่าเหล่านี้

```ini
LLM_BASE_URL=http://<ip-llm>:<port>/v1
LLM_API_KEY=none          # ถ้า server ไม่มี auth ให้ใส่ none ห้ามเว้นว่าง (ว่าง = ปิด LLM)
LLM_MODEL=<ชื่อ model ตามที่ /v1/models แสดง>
EMBEDDING_BASE_URL=http://<ip-embedding>:8090/v1
LLM_APP_URL=https://<โดเมนที่ผู้ใช้เปิด>
COOKIE_SECURE=true        # ถ้ายังไม่มี HTTPS ให้ใช้ false ไปก่อน
```

> **ห้ามใช้ `localhost`** เพราะ container จะมองไม่เห็นเครื่องจริง ให้ใช้ IP หรือชื่อเครื่องแทน ค่าอื่นปล่อยตามเดิมได้

ถ้าใช้ OpenRouter: `LLM_BASE_URL=https://openrouter.ai/api/v1`, ใส่ API key จริงใน `LLM_API_KEY` และเว้น `LLM_API_DIALECT=` ว่าง

## 4. กลับไปกด Enter ที่หน้าต่างตัวติดตั้ง

script จะถาม Harbor username และ token ให้ใส่ค่าที่ได้รับจาก Softnix จากนั้นจะ pull image เตรียมฐานข้อมูล เปิดระบบ และตรวจบริการให้เองทั้งหมด

## 5. เข้าสู่ระบบ

- ตอนจบ script จะแสดงรหัส admin ให้ดู **ครั้งเดียว** ให้จดเก็บไว้ทันที
- เปิด `http://<ip-server>:8081` (ค่า `WEB_PORT` เริ่มต้น) แล้ว login ด้วย `admin` และรหัสนั้น
- หลัง login ครั้งแรกให้เปลี่ยนรหัสทันที

## 6. ก่อนใช้งานจริง

- ตั้ง HTTPS reverse proxy ไว้หน้าพอร์ต 8081 เช่น nginx, Caddy หรือ Cloudflare Tunnel
- อย่าเปิดพอร์ตของ Postgres, Redis, Neo4j และ LightRAG ออกสู่ภายนอก

## อัปเกรดเป็นเวอร์ชันใหม่

```bash
bash deploy/backup.sh
bash deploy/install.sh --version v1.0.1
```

## ติดตั้งแบบไม่มีอินเทอร์เน็ต (offline)

1. บนเครื่องที่ต่อ Harbor ได้ รัน `bash deploy/offline-bundle.sh save v1.0.0 linux/amd64` (ได้ไฟล์ประมาณ 4 GB)
2. ย้ายไฟล์ไปเครื่องลูกค้า แตกไฟล์ แล้วรัน `bash deploy/offline-bundle.sh load`
3. รัน `bash deploy/install.sh --offline`

## ถ้ามีปัญหา

```bash
docker compose logs api migrate lightrag
bash deploy/check_services.sh
```
