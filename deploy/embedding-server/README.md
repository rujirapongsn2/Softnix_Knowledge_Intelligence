# Embedding server (llama.cpp + embeddinggemma)

api, worker และ LightRAG เรียก embedding ผ่าน OpenAI-compatible endpoint `POST {EMBEDDING_BASE_URL}/embeddings`
ค่าเริ่มต้นคือ `llama-server` ของ llama.cpp รันโมเดล `google/embeddinggemma-2` (768 มิติ) บนเครื่อง host
เปลี่ยน host, โมเดล, key หรือ prefix ได้จาก `.env` อย่างเดียว ไม่ต้องแก้โค้ด

## ติดตั้งและรัน

```sh
brew install llama.cpp
llama-server -hf ggml-org/embeddinggemma-2-GGUF:BF16 --no-mmproj --embeddings \
  --host 0.0.0.0 --port 8090 -ngl 99 -c 32768 -b 8192 -ub 8192 --parallel 4 --api-key <key>
```

- `-b`/`-ub 8192` คือความยาวสูงสุดของ input หนึ่งชิ้น (token) ชิ้นที่ยาวกว่านี้ได้ HTTP 500 และเอกสารจะขึ้น `EMBEDDING_REQUEST_TOO_LARGE`
- `-c` ถูกแบ่งให้แต่ละ slot เท่าๆ กัน (`-c 32768 --parallel 4` ได้ slot ละ 8192 token) ถ้าใช้ `-c 8192` แต่ละ slot ได้แค่ 2048 token และคำอธิบาย entity ที่ยาวของ LightRAG จะ embed ไม่ผ่าน
- `--api-key` ไม่บังคับ ถ้าไม่ใส่ ให้ปล่อย `EMBEDDING_API_KEY` ว่าง ถ้าเปิดพอร์ตให้เครื่องอื่นเข้าถึง ควรตั้ง key เสมอ

ทดสอบ:

```sh
curl -s http://127.0.0.1:8090/v1/embeddings -H 'Content-Type: application/json' -H 'Authorization: Bearer <key>' \
  -d '{"model":"embeddinggemma-2","input":["ทดสอบ"]}' | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["data"][0]["embedding"]))'
# 768
```

## ให้รันตลอดบน macOS (launchd)

บันทึกเป็น `~/Library/LaunchAgents/com.softnix.embedding-server.plist` แล้วโหลดด้วย
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.softnix.embedding-server.plist`
(หยุดด้วย `launchctl bootout gui/$(id -u)/com.softnix.embedding-server`)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.softnix.embedding-server</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/llama-server</string>
    <string>-hf</string><string>ggml-org/embeddinggemma-2-GGUF:BF16</string>
    <string>--no-mmproj</string>
    <string>--embeddings</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>8090</string>
    <string>-ngl</string><string>99</string>
    <string>-c</string><string>32768</string>
    <string>-b</string><string>8192</string>
    <string>-ub</string><string>8192</string>
    <string>--parallel</string><string>4</string>
    <string>--api-key</string><string>REPLACE_WITH_KEY</string>
  </array>
  <!-- -hf downloads into ~/Library/Caches/llama.cpp, so launchd needs HOME. -->
  <key>EnvironmentVariables</key>
  <dict><key>HOME</key><string>/Users/REPLACE_WITH_USER</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/embedding-server.log</string>
  <key>StandardErrorPath</key><string>/tmp/embedding-server.log</string>
</dict>
</plist>
```

## ตั้งค่า `.env`

```sh
# server บนเครื่องเดียวกับ docker (Docker Desktop บน macOS/Windows)
EMBEDDING_BASE_URL=http://host.docker.internal:8090/v1
# หรือ server บนเครื่องอื่น
# EMBEDDING_BASE_URL=http://<host>:8090/v1
EMBEDDING_API_KEY=<key>
EMBEDDING_MODEL=embeddinggemma-2
EMBEDDING_DIMENSION=768
EMBEDDING_QUERY_PREFIX="task: search result | query: "
EMBEDDING_DOCUMENT_PREFIX="title: none | text: "
```

บน Linux `host.docker.internal` ไม่มีให้โดยอัตโนมัติ ต้องเพิ่ม `extra_hosts: ["host.docker.internal:host-gateway"]` ให้ api, worker และ lightrag หรือใช้ IP ของ host แทน

ตรวจการเชื่อมต่อจาก UI หรือ `POST /api/v1/system/test-openrouter` ผลลัพธ์มี `embedding_model_available`, `embedding_dimension`, `embedding_host` และ `embedding_error` เมื่อเรียกไม่สำเร็จ

## เปลี่ยนโมเดลหรือจำนวนมิติ

vector เดิมใช้กับโมเดลใหม่ไม่ได้ ต้องทำครบทั้งสามขั้น:

1. ตั้ง `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` ใน `.env` แล้วรัน migration (`docker compose run --rm migrate`)
   migration `0039_embedding_dimension` ปรับคอลัมน์ `document_chunks.embedding` ให้เป็น `vector(EMBEDDING_DIMENSION)`
   และล้าง embedding เดิมทิ้ง ถ้ามิติตรงกันอยู่แล้วจะไม่ทำอะไร
   ถ้า 0039 ถูก apply ไปแล้วและต้องการเปลี่ยนมิติอีกครั้ง ให้รัน
   `alembic downgrade 0038_processing_job_claims && alembic upgrade head`
2. สั่ง Reindex ทุก Knowledge Base (`POST /api/v1/knowledge-bases/{id}/documents/reindex`) เพื่อสร้าง embedding ใหม่
3. ingest เอกสารเข้า LightRAG ใหม่ Reindex ในข้อ 2 ไม่แตะ LightRAG และ vector store ของ LightRAG ยังเก็บ vector ของโมเดลเดิม
   จึงต้องล้างตาราง `lightrag_*` และ node label `lightrag` แล้วรัน `deploy/lightrag-storage/reingest.py`
   (`rebuild_lightrag_vectors.py` ใช้ได้กับ storage แบบไฟล์เท่านั้น)
   (LightRAG ใส่ prefix `EMBEDDING_*_PREFIX` ไม่ได้ จึงใช้ vector แบบไม่มี prefix)
