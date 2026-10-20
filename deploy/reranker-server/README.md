# Reranker server (llama.cpp + bge-reranker-v2-m3)

หลังรวมผลจากทุกช่องทางค้นหาแล้ว api ส่ง candidate (ค่าเริ่มต้น 20 ชิ้น, `RERANK_CANDIDATE_LIMIT`) ไปเรียงใหม่ที่
`POST {RERANK_BASE_URL}/rerank` แบบ Cohere ซึ่ง `llama-server --reranking` รองรับ ถ้า server ไม่ตอบ ระบบใช้ลำดับเดิมต่อ
และบันทึกใน trace ว่า `RERANKER_UNAVAILABLE`

ผลวัดบน M4 (2026-10-20):

- **rerank 30 อันดับแรกจาก embeddinggemma** บนคำถามภาษาไทย 190 ข้อ กับ chunk เอกสารกฎหมาย 2,562 ชิ้น:
  Recall@10 จาก 0.726 เป็น 0.789 และ Recall@5 จาก 0.637 เป็น 0.711
- **ผ่าน `/api/v1/query` จริง** แบบ `response_mode: "evidence"` ทีละ KB ใช้ 90 คำถามที่คำตอบอยู่ในเอกสารที่ไม่ถูกแทนที่
  นับว่าเจอเมื่อ source เป็น chunk เป้าหมายหรือข้างเคียง หรือมีเนื้อหาเหมือนกันจากเอกสารฉบับอื่น:
  Recall@1 จาก 0.256 เป็น 0.500, Recall@5 จาก 0.422 เป็น 0.644 และ MRR@10 จาก 0.339 เป็น 0.559
  Recall@10 เท่าเดิม (0.644) เพราะ rerank เรียงใหม่เฉพาะ candidate ที่ fusion หามาได้
- **เวลา rerank** (20 candidate) p50 342 ms และ p95 730 ms เวลาตอบรวมแบบ evidence จาก p50 1.8 เป็น 2.4 วินาที

## ติดตั้งและรัน

```sh
brew install llama.cpp
sed "s#/Users/REPLACE_WITH_USER#$HOME#g" deploy/reranker-server/ai.softnix.reranker-server.plist \
  > ~/Library/LaunchAgents/ai.softnix.reranker-server.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.softnix.reranker-server.plist
# หยุด: launchctl bootout gui/$(id -u)/ai.softnix.reranker-server
```

ครั้งแรกจะดาวน์โหลดโมเดล (ราว 600 MB) จาก Hugging Face server ฟังที่ `127.0.0.1:8091`
ส่วน container เรียกผ่าน `host.docker.internal:8091` ถ้าเปิดให้เครื่องอื่นใช้ ให้เปลี่ยนเป็น `--host 0.0.0.0` และเพิ่ม `--api-key`

ทดสอบ:

```sh
curl -s http://127.0.0.1:8091/v1/rerank -H 'Content-Type: application/json' \
  -d '{"model":"bge-reranker-v2-m3","query":"อายุความฟ้องคดี","documents":["ร้านกาแฟเปิดแปดโมง","คดีมีอายุความสิบปี"]}'
# results[0].index == 1
```

## ตั้งค่า `.env`

```sh
RERANKER_ENABLED=true
RERANK_MODEL=bge-reranker-v2-m3
RERANK_BASE_URL=http://host.docker.internal:8091/v1
RERANK_API_KEY=
```

## ใช้ reranker บนเครื่องอื่น

แก้ `.env` อย่างเดียว ไม่ต้องแก้โค้ด แล้วสร้าง api และ worker ใหม่ (`docker compose up -d api worker`):

```sh
# server แบบ Hugging Face text-embeddings-inference (body {"query", "texts"} ตอบเป็น [{"index", "score"}])
RERANK_BASE_URL=https://<host>          # ระบบต่อท้ายด้วย /rerank
RERANK_API_STYLE=tei                    # cohere (ค่าเริ่มต้น) หรือ tei; tei ไม่ใช้ RERANK_MODEL
RERANK_API_KEY=<key>                    # ส่งเป็น Authorization: Bearer
RERANK_VERIFY_SSL=false                 # เฉพาะ certificate ที่ self-signed ในเครือข่ายที่เชื่อถือได้
```

วัดเทียบกับ server TEI เครื่องหนึ่งในเครือข่ายด้วยคำถาม 190 ข้อชุดเดียวกัน (30 candidate):
คุณภาพใกล้กันจนแยกไม่ออก (Recall@10 lenient 0.789 ทั้งคู่; MRR@10 0.594 กับ 0.577 ของเครื่องนี้)
แต่เร็วกว่าที่ p50 128 ms กับ 2,333 ms ส่วนต่างนี้เหลือราว 0.2 วินาทีบนคำถามจริงที่ใช้เวลารวมราว 9 วินาที
ค่าเริ่มต้นจึงยังเป็นเครื่องนี้ ข้อความเอกสารและคำถามไม่ต้องออกจากเครื่อง

ถ้า `RERANK_BASE_URL` ว่าง ระบบจะส่ง rerank ไปที่ `LLM_BASE_URL` ด้วย key ของ LLM
และปิด rerank เฉพาะบาง KB ได้จากนโยบาย retrieval ของ KB นั้น
