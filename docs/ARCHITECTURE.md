# สถาปัตยกรรม

ระบบแบ่งเป็น API สำหรับ REST/MCP, Web UI, worker สำหรับงานเบื้องหลัง และบริการข้อมูลเฉพาะด้าน โดย PostgreSQL เป็นแหล่งข้อมูลอ้างอิงหลักของ metadata, chunk, vector, full-text, citation และ audit

## บทบาทของส่วนประกอบ

| ส่วนประกอบ | หน้าที่ | ขอบเขตความน่าเชื่อถือ |
|---|---|---|
| Web / FastAPI | UI, REST, login และ MCP | ตรวจสิทธิ์และขอบเขตทุก request |
| Redis | คิวงานและ coordination | ไม่ใช่แหล่งข้อมูลถาวรหลัก |
| Worker | extract, chunk, embed, graph projection และ reindex | ทำงาน asynchronous และบันทึกสถานะ job |
| PostgreSQL + pgvector | metadata, chunks, vector, FTS, provenance, legal registry, trace/audit | แหล่งข้อมูลหลักและแหล่ง citation |
| Neo4j | entity/relationship projection และ bounded traversal | accelerator; PostgreSQL ยังเป็น authority |
| LightRAG | semantic/graph retrieval เสริม | adapter เท่านั้น ไม่ใช่ source of truth |
| OpenRouter | embedding, LLM fallback/answer และ optional rerank | external dependency; รับเฉพาะ evidence ที่กรองแล้ว |

ไฟล์ต้นฉบับเก็บใน volume `files` นอก web root และไม่ส่งให้ query client โดยตรง

## Ingestion

Upload จะตรวจ MIME, ขนาด และ checksum ก่อนสร้าง job `PROCESS_DOCUMENT` ใน Redis จากนั้น worker ใช้ MarkItDown แปลงเป็น Markdown, normalize และแบ่ง chunk แบบ overlap เก็บ vector/full-text/provenance ใน PostgreSQL พร้อมฉาย entity/relationship ไป Neo4j และ sync LightRAG

PDF ที่ไม่มี text layer จะเรียก external OCR หากตั้งค่าไว้ หากไม่ตั้งค่าจะจบด้วย `OCR_REQUIRED` เพื่อให้ผู้ดูแลแก้ไขก่อนประมวลผลต่อ

Document Type คือ metadata contract ของผู้ดูแล ส่วน Processing Profile (`general`, `legal`, `regulation`, `contract`) คือพฤติกรรมการประมวลผลที่เลือกได้จำกัด Custom Type จะสืบทอด field พื้นฐานจาก profile และเก็บ snapshot ไว้กับเอกสารทุกฉบับ จึงแก้ template ภายหลังได้โดยไม่ทำให้เอกสารเก่าเปลี่ยนความหมายเอง Field ใหม่จะถูกนำไปค้น, ใช้เป็น filter หรือสร้าง graph ก็ต่อเมื่อเปิด capability นั้นอย่างชัดเจน

## Auto Retrieval

`RetrievalPlan` ถูกสร้างด้วย rule-first planner และเก็บ intent, channels, graph scope/depth, entity/document identifiers, publication date range และ `rerank_enabled` ไว้ใน response, audit และ trace

- Exact: ค้นชื่อเอกสาร, original filename และเลขเอกสารใน chunk
- Vector: pgvector cosine similarity จาก embedding 1536 มิติ
- Full-text: PostgreSQL FTS และ fallback แบบ case-insensitive สำหรับข้อความไทย
- Graph: local จาก entity/alias ที่ resolve ได้ หรือ global ที่สรุปหลักฐานจากหลาย connected components
- Metadata filter: กรองจาก field snapshot แบบ exact match ก่อนส่งเข้า vector, full-text, exact และ graph evidence
- LightRAG: ใช้เป็น retrieval เสริมเมื่อ policy เปิดใช้งาน

Executor รัน channel ที่เลือกแบบขนาน, เคารพ policy ของ Knowledge Base, กรองสิทธิ์และ date range ทุก channel, รวมอันดับ, เรียก reranker เฉพาะเมื่อเปิดใช้งาน และคืนหลักฐานพร้อม citation หาก channel ใดล้มเหลว channel อื่นยังส่งผลได้

## Graph และ legal registry

PostgreSQL เก็บ entity/relationship และ evidence เป็น authority; Neo4j รับ projection ผ่าน outbox event และ retry ได้ หาก Neo4j ล่มหรือข้อมูลไม่ครบ ระบบ fallback เป็น PostgreSQL traversal

เอกสารกฎหมายมี registry แยกสำหรับ family, instrument, effective dates และ status ตัว resolver เป็น deterministic และเปลี่ยนสถานะจากความสัมพันธ์ที่ผ่าน human review แล้วเท่านั้น การ override ของผู้ดูแลจะไม่ถูก resolver ทับ

เอกสารกฎหมายฉบับรวม (consolidated) หนึ่งไฟล์อาจมีหลายส่วนย่อยที่เลขมาตราชนกัน (เช่น พ.ร.บ.ให้ใช้ฯ, ตัวประมวลกฎหมาย, และหมายเหตุท้ายฉบับแก้ไข) ระบบจึงแบ่ง chunk เป็น sub-work ตามลำดับก่อนตอบคำถามที่มีเลขมาตราชนกัน และการหาฉบับแก้ไขล่าสุดของมาตราหนึ่ง (amendment attribution) ถูกจำกัดขอบเขตไว้เฉพาะ legal family เดียวกัน เพื่อไม่ให้กฎหมายคนละฉบับที่บังเอิญมีเลขมาตราเดียวกันถูกอ้างอิงผิด

## Observability

`trace_runs`/`trace_spans` ใช้สำหรับ Trace Explorer และ `audit_logs` เก็บเหตุการณ์สำคัญ โดยไม่เก็บ bearer token, authorization header, full prompt หรือ document body ระบบมี retention แยกสำหรับ trace/request/MCP และ audit ตามค่าใน `.env`

Trace Explorer แสดงทั้ง MCP tool call ที่สำเร็จ (พร้อม retrieval plan และ channel trace เต็มรูปแบบ) และที่ถูกปฏิเสธ/ล้มเหลว (rate limit, tool ไม่ได้รับอนุญาต, timeout, invalid request) — แต่ละ error code จะถูกบันทึกเป็น trace แยกต่างหากแม้ไม่มี retrieval plan ให้แสดง เพื่อให้ผู้ดูแลตรวจสอบ MCP integration ที่มีปัญหาได้จากหน้า Logging เพียงที่เดียว
