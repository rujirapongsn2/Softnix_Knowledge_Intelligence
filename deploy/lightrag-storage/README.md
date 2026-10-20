# LightRAG storage (Postgres + Neo4j)

LightRAG เก็บข้อมูลใน Postgres (ตาราง `lightrag_*` ในฐานข้อมูล `skip`, workspace `lightrag`) และ Neo4j (node label `lightrag`)
ค่าเริ่มต้นใน `docker-compose.yml` คือ `PGKVStorage`, `PGVectorStorage`, `PGDocStatusStorage` และ `Neo4JStorage`

storage แบบไฟล์เดิม (JSON, NanoVectorDB, NetworkX) โหลดทุก store เข้าหน่วยความจำและเขียนทับทั้งไฟล์ทุกครั้งที่ ingest หนึ่งเอกสาร
จึงรับเอกสารได้ไม่เกินหลักร้อย ไฟล์เดิมยังอยู่ใน volume `lightrag` และไม่ถูกแก้ไข

## ย้ายจาก storage แบบไฟล์

```sh
bash deploy/lightrag-storage/cutover.sh            # backup ไว้ที่ ~/softnix-backups/<วันที่>-lightrag-storage
```

สิ่งที่ script ทำตามลำดับ:

1. ตรวจว่า embedding server ตอบ และ `.env` ไม่ได้ตั้ง `LIGHTRAG_*_STORAGE` ทับค่าเริ่มต้น
2. backup Postgres (`pg_dump`) และ volume `lightrag`
3. หยุด LightRAG แล้วคัดลอก LLM cache ส่วน extract และ summary เข้า Postgres (`copy_llm_cache.py`)
4. เปิด LightRAG บน storage ใหม่ และตรวจจาก `/health` ว่าใช้ storage ทั้งสี่ตัวจริง
5. ส่งข้อความที่สกัดไว้แล้วของทุกเอกสารที่ค้นหาได้เข้า LightRAG ใหม่ (`reingest.py`)
   ไม่สกัดข้อความใหม่ ไม่สร้าง chunk หรือ embedding ใหม่ และไม่เรียก LLM สกัด metadata หรือข้อมูลกฎหมาย
   เอกสารที่ LightRAG มีแล้วจะนับว่าสำเร็จ จึงรันซ้ำได้
6. บันทึกหน่วยความจำก่อนและหลัง ขนาดตาราง `lightrag_*` และจำนวน node ใน Neo4j

ระหว่างขั้นที่ 5 การค้นหาผ่าน LightRAG ได้ผลไม่ครบ ส่วนการค้นหาจาก pgvector และ full-text ของแอปทำงานตามปกติ

LLM cache ช่วยได้เฉพาะ chunk ที่ข้อความตรงกับครั้งก่อนทุกตัวอักษร ถ้าข้อความที่ส่งเข้า LightRAG เปลี่ยนไปหลัง ingest ครั้งแรก
LightRAG จะเรียก LLM ใหม่ทั้ง chunk ในการย้ายครั้งแรกบนเครื่อง dev cache ไม่ช่วยเลย และ re-ingest ใช้ค่า LLM เต็มจำนวน

re-ingest เฉพาะ KB เดียว:

```sh
docker compose exec -T api python - --knowledge-base <KB_ID> --workers 2 < deploy/lightrag-storage/reingest.py
```

## ย้อนกลับ

```sh
bash deploy/lightrag-storage/rollback.sh
```

script จะตั้ง `LIGHTRAG_*_STORAGE` ใน `.env` กลับเป็น storage แบบไฟล์ แล้วสร้าง container LightRAG ใหม่
เอกสารที่ upload หลังการย้ายไม่มีอยู่ในไฟล์เดิม ต้องกด "ประมวลผลใหม่" ให้เอกสารเหล่านั้น

## ทดสอบนำเข้าเป็นขั้น

`stage_test.py` ส่งไฟล์จากโฟลเดอร์ผ่าน Ingest API ทีละขั้น รอจนทุกไฟล์ประมวลผลเสร็จ แล้วสรุปผลดังนี้:

- จำนวนเอกสารต่อชั่วโมง และอัตราการล้ม
- หน่วยความจำสูงสุดของ LightRAG, Postgres และ Neo4j
- ขนาดตาราง `lightrag_*`
- ค่า OpenRouter ที่ใช้ไป (ต้องตั้ง `OPENROUTER_API_KEY`)

```sh
INGEST_TOKEN=<ingest token ของ KB ทดสอบ> OPENROUTER_API_KEY=<key> \
  python3 deploy/lightrag-storage/stage_test.py --kb <KB_ID> --dir <โฟลเดอร์เอกสาร> --count 100
```

ไฟล์ที่ส่งแล้วถูกบันทึกใน `<โฟลเดอร์>/.stage-state.json` ครั้งถัดไปจึงส่งเฉพาะไฟล์ที่ยังไม่ได้ส่ง
ให้เพิ่ม `--count` ทีละขั้น เช่น 100, 400 แล้ว 1000 และเทียบผลสรุปของแต่ละขั้นก่อนส่งเพิ่ม
ใช้ KB แยกสำหรับทดสอบ และถ้าไม่ต้องการค่า LLM สกัด metadata อย่าผูกประเภทเอกสารที่มีฟิลด์ให้สกัด

ทุกเอกสารอยู่ในคิวเดียวกับงานอื่น (คิวแบบมาก่อนได้ก่อน) ระหว่างทดสอบ เอกสารที่ผู้ใช้ upload จะรอจนงานทดสอบเสร็จ
