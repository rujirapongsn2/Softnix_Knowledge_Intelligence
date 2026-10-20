# Reranker server (llama.cpp + bge-reranker-v2-m3)

หลังรวมผลจากทุกช่องทางค้นหาแล้ว api ส่ง candidate (ค่าเริ่มต้น 20 ชิ้น, `RERANK_CANDIDATE_LIMIT`) ไปเรียงใหม่ที่
`POST {RERANK_BASE_URL}/rerank` แบบ Cohere ซึ่ง `llama-server --reranking` รองรับ ถ้า server ไม่ตอบ ระบบใช้ลำดับเดิมต่อ
และบันทึกใน trace ว่า `RERANKER_UNAVAILABLE`

วัดกับคำถามภาษาไทย 190 ข้อบน chunk เอกสารกฎหมาย 2,562 ชิ้น (rerank 30 อันดับแรกจาก embeddinggemma):
Recall@10 จาก 0.726 เป็น 0.789 และ Recall@5 จาก 0.637 เป็น 0.711 ใช้เวลา rerank ราว 2.5 วินาทีต่อคำถาม (p50) บน M4

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

ถ้า `RERANK_BASE_URL` ว่าง ระบบจะส่ง rerank ไปที่ `LLM_BASE_URL` ด้วย key ของ LLM
และปิด rerank เฉพาะบาง KB ได้จากนโยบาย retrieval ของ KB นั้น
