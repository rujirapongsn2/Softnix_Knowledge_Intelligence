# การเชื่อมต่อ MCP

ใช้ Streamable HTTP JSON-RPC ที่ `POST /mcp` พร้อม header:

```http
Authorization: Bearer skik_live_...
```

สร้าง token จากหน้า **MCP Tokens** หรือ `POST /api/v1/tokens` ระบบแสดง secret ครั้งเดียวและเก็บเฉพาะ HMAC-SHA-256 digest

เครื่องมือ MCP ทั้งหมดเป็นแบบอ่านเท่านั้น และ token เรียกได้เฉพาะ tool ที่ระบุไว้ใน `allowed_tools`
(รายการว่างหมายถึงไม่ได้สิทธิ์ tool ใดเลย) ถ้าต้องการให้ระบบภายนอก**นำเข้า**เอกสาร ให้ใช้
[Ingestion API](INGEST_API.md) ซึ่งเป็น scope แยก (`documents:write`) และไม่ผ่าน `/mcp`

## เครื่องมือที่รองรับ

`search_knowledge`, `describe_knowledge_schema`, `document_inventory_summary`, `find_entities`, `analyze_relationships`, `analyze_impact`, `get_sources`, `resolve_legal_context`, `get_legal_instrument`, `get_provision_history`

ใช้ `document_inventory_summary` เมื่อผู้ใช้ถามจำนวนเอกสาร รายการทั้งหมด หรือการแบ่งกลุ่มตามประเภท เครื่องมือนี้อ่านจาก document/legal registry โดยตรงและไม่ใช้ LLM นับจาก chunks:

```json
{
  "query": "ชุดเอกสารนี้มีกฎหมายทั้งหมดกี่ฉบับ และแบ่งเป็นประเภทใดบ้าง?",
  "scope": "all",
  "include_documents": true,
  "max_documents": 500
}
```

`scope=all` หมายถึงเอกสารที่ไม่ถูกลบทั้งหมดในขอบเขตของ token ส่วน `scope=current` ใช้เมื่อต้องการเฉพาะรายการที่ยังมีผล/ยังไม่ถูกแทนที่ ผลลัพธ์มี `total_documents`, `groups`, `documents` และ citation แบบ `[I#]` สำหรับกลุ่มสรุป

สำหรับ `search_knowledge` ให้ส่งคำถามของผู้ใช้แบบเดิมโดยไม่ tokenize หรือ rewrite เอง หากเป็นคำถามนับ/แบ่งประเภท ให้เลือก `document_inventory_summary` แทนการนับจากจำนวน citations

`tools/list` จะแสดงเฉพาะเครื่องมือที่ token อนุญาต Token scope ของ Knowledge Base เป็น authority; client ไม่สามารถส่ง ID เพิ่มเพื่อขยายสิทธิ์ได้ เครื่องมือกฎหมายเป็น read-only และความสัมพันธ์ที่ยังไม่ review จะไม่ถูกนำเสนอเป็นข้อเท็จจริงยืนยันแล้ว

Token เดิมที่สร้างก่อนมี `document_inventory_summary` จะยังไม่ถูกเพิ่มสิทธิ์ให้อัตโนมัติเพื่อไม่ขยายสิทธิ์โดยไม่ตั้งใจ ให้สร้าง/หมุน token ใหม่และเลือกเครื่องมือนี้ หรือใช้ `search_knowledge` กับคำถามต้นฉบับ ซึ่ง server มี deterministic inventory fallback สำหรับคำถามนับ/แบ่งประเภท

Token มี expiry, enable/disable/revoke, rate limit, concurrency limit และ query timeout หากถูกปฏิเสธจะได้ JSON-RPC error code เช่น `MCP_RATE_LIMITED`, `MCP_TIMEOUT`, `AUTH_TOOL_NOT_ALLOWED` หรือ `KNOWLEDGE_BASE_INACTIVE`

## Claude Code

```bash
claude mcp add --transport http softnix-knowledge "https://your-softnix-host/mcp" \
  --header "Authorization: Bearer skik_live_..."
```

ใช้ `/mcp` ใน Claude Code เพื่อตรวจการเชื่อมต่อ และเก็บ token ผ่าน environment variable เช่น `${SOFTNIX_MCP_TOKEN}` แทนการเขียนลง repository

## Agent Skill (SKILL.md)

หน้า **MCP Tokens** มีปุ่ม "Copy SKILL" ที่สร้างไฟล์ `SKILL.md` ตามมาตรฐานเปิด [agentskills.io](https://agentskills.io) — ใช้ได้กับ Claude Code และ agent tool อื่นที่รองรับมาตรฐานเดียวกัน (Cursor, Gemini CLI, VS Code, GitHub Copilot ฯลฯ) เนื้อหาของ Skill สั่งให้ agent ตอบคำถามจาก Knowledge Base ที่ token ผูกไว้เท่านั้น ห้ามใช้ web search, web fetch หรือ training data ของตัวเอง เพื่อป้องกันคำตอบที่ผสมแหล่งข้อมูลอื่นโดยผู้ใช้ไม่ทราบ

บันทึกไฟล์ที่ได้ไว้ที่ `SKILL.md` ในโฟลเดอร์ชื่อ `softnix-knowledge` ภายใต้ skills directory ของ agent — สำหรับ Claude Code คือ `.claude/skills/softnix-knowledge/SKILL.md`

## Custom metadata อัตโนมัติ

เพิ่มสิทธิ์ `describe_knowledge_schema` ให้ token ใหม่เมื่อต้องการให้ Agent อ่านประเภทเอกสาร นิยาม field และ coverage ภายใน KB ที่อนุญาต เครื่องมือนี้ไม่ถูกเพิ่มให้ token เดิมอัตโนมัติ ใช้ `offset` / `limit` และตาม `next_offset` เพื่ออ่าน snapshot เก่าให้ครบ

`search_knowledge` และ `document_inventory_summary` รองรับ `filters.metadata_predicates` แบบมี template identity และชนิดข้อมูลจริง เช่นช่วงวันที่/ตัวเลข โดยยังรองรับ `filters.metadata` เดิม ค่า unknown ไม่ผ่าน explicit filters และระบบไม่คลาย filter เอง ผลการค้นหามี metadata provenance และ coverage เพื่อไม่ให้ Agent สรุปว่าข้อมูลที่ยังอ่านไม่ครบคือไม่มีเอกสาร

ดู [รูปแบบ API, ข้อจำกัด และวิธีเปิดใช้](AUTO_METADATA_IMPLEMENTATION.md)

## Citations และดาวน์โหลดไฟล์ต้นฉบับ

ผลลัพธ์จาก `search_knowledge` ใช้ contract `ski.answer.v1` ใน `result.structuredContent` เพื่อให้ Agent และ frontend อ่านข้อมูลโดยไม่ต้อง parse ข้อความ คีย์หลักคือ:

- `answer` — คำตอบสำหรับแสดงผล
- `claims[]` — ข้อความย่อยพร้อม `reference_ids[]` ที่รองรับแต่ละ claim
- `references[]` — แหล่งอ้างอิงรูปแบบคงที่สำหรับ citation card, evidence drawer และ PDF preview
- `sources[]` — ข้อมูล retrieval เดิมสำหรับ backward compatibility; frontend ใหม่ควรใช้ `references`

แต่ละ `references[]` มี `quality` ตาม contract `ski.quality.v1` เพื่อให้ Agent และ frontend ประเมินความพร้อมของหลักฐานได้โดยไม่เดาจากสถานะ processing:

- `status` — `not_queryable`, `needs_review`, `ai_ready` หรือ `verified`
- `score` และ `dimensions` — คะแนนรวมและคะแนน content, structure, metadata, retrieval, citation และ graph
- `blockers[]` — ปัญหาที่ทำให้เอกสารยังไม่ควรถูกใช้ตอบ
- `warnings[]` — เอกสารยังใช้ได้ แต่ Agent ควรแจ้งข้อจำกัดหรือเลือกหลักฐานที่ดีกว่า

Agent ต้องไม่ใช้ reference ที่เป็น `not_queryable`; สำหรับ `needs_review` ให้แสดงข้อจำกัดจาก `warnings` และยังต้องอ้างอิงข้อความหลักฐานตามปกติ คะแนนคุณภาพใช้ประกอบการเลือกแหล่งข้อมูล ไม่ใช้แทน relevance ของคำถาม

ตัวอย่าง:

```json
{
  "schema_version": "ski.answer.v1",
  "answer": "ข้อกำหนดระบุให้ดำเนินการตามขั้นตอน [S1]",
  "claims": [
    {
      "id": "C1",
      "text": "ข้อกำหนดระบุให้ดำเนินการตามขั้นตอน",
      "citation_ids": ["S1"],
      "reference_ids": ["R1"]
    }
  ],
  "references": [
    {
      "id": "R1",
      "citation_id": "S1",
      "document_id": "DOCUMENT_ID",
      "title": "ข้อกำหนดตัวอย่าง",
      "file": {
        "available": true,
        "name": "rule.pdf",
        "mime_type": "application/pdf",
        "download_url": "https://knowledge.softnix.ai/api/v1/documents/DOCUMENT_ID/file",
        "access": {
          "method": "GET",
          "authentication": "bearer_or_session",
          "disposition_parameter": "inline|attachment"
        }
      },
      "locator": {
        "chunk_id": "CHUNK_ID",
        "section": "ข้อ 4",
        "section_kind": "ข้อ",
        "section_number": "4",
        "page_start": null,
        "page_end": null
      },
      "excerpt": "ข้อความหลักฐานที่ใช้ตอบคำถาม"
    }
  ]
}
```

JSON Schema อยู่ที่ [`docs/schemas/ski-answer-v1.schema.json`](schemas/ski-answer-v1.schema.json) และ `get_sources` จาก `result_id` เดิมจะคืน `answer`, `claims`, `references` และ `sources` ใน contract เดียวกัน

แหล่งอ้างอิงมีข้อมูลไฟล์ต้นฉบับเมื่อไฟล์ยังอยู่ในระบบ:

- `original_filename` — ชื่อไฟล์ตอนอัปโหลด
- `mime_type` — เช่น `application/pdf`
- `download_url` — path แบบ root-relative เช่น `/api/v1/documents/{document_id}/file` (หรือ absolute เมื่อตั้ง `PUBLIC_APP_URL`)

ในบล็อกข้อความ «รายละเอียดแหล่งอ้างอิง» ของคำตอบ: ระบบใส่ URL ไฟล์ใน SKI (`download_url`) เมื่อมีไฟล์ต้นฉบับที่เก็บไว้ (PDF หรือข้อความ) — **ไม่ใช้**ลิงก์ OCS / searchlaw / council-of-state จาก `source_uri` ใน citation prose; ถ้าไม่มี `download_url` และไม่มี `source_uri` ที่ปลอดภัย จะแสดงเฉพาะชื่อเอกสาร

Agent backend ต้องเรียก `GET references[].file.download_url` พร้อม header เดียวกับ MCP:

```http
Authorization: Bearer skik_live_...
```

ไม่ฝังไบต์ของ PDF ใน JSON-RPC — ใช้ลิงก์ดาวน์โหลดที่ควบคุมด้วย token เท่านั้น Token ต้องมี Knowledge Base ของเอกสารนั้นในขอบเขตอ่าน (ไม่ต้องมี `documents:write`) เอกสารที่ soft-delete แล้วจะได้ 404 การดาวน์โหลดด้วย session ของแอดมินและ Ingest API (`GET /api/v1/ingest/documents/{id}/file`) ยังทำงานเหมือนเดิม

ห้ามส่ง MCP token ไปเก็บหรือเรียกใช้จาก browser frontend ให้ application backend proxy ไฟล์ด้วยสิทธิ์ของผู้ใช้ หรือใช้ session ของ SKI สำหรับ browser ที่ลงชื่อเข้าใช้แล้ว ใช้ `?disposition=inline` สำหรับ PDF preview และ `?disposition=attachment` สำหรับดาวน์โหลด

ระบบนี้ยัง**ห้าม** web search / web fetch นอก Knowledge Base ที่ token อนุญาต
