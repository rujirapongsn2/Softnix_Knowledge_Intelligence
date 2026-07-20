# การเชื่อมต่อ MCP

ใช้ Streamable HTTP JSON-RPC ที่ `POST /mcp` พร้อม header:

```http
Authorization: Bearer skik_live_...
```

สร้าง token จากหน้า **Access & MCP** หรือ `POST /api/v1/tokens` ระบบแสดง secret ครั้งเดียวและเก็บเฉพาะ HMAC-SHA-256 digest

## เครื่องมือที่รองรับ

`search_knowledge`, `document_inventory_summary`, `find_entities`, `analyze_relationships`, `analyze_impact`, `get_sources`, `resolve_legal_context`, `get_legal_instrument`, `get_provision_history`

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

หน้า **Access & MCP** มีปุ่ม "Copy SKILL" ที่สร้างไฟล์ `SKILL.md` ตามมาตรฐานเปิด [agentskills.io](https://agentskills.io) — ใช้ได้กับ Claude Code และ agent tool อื่นที่รองรับมาตรฐานเดียวกัน (Cursor, Gemini CLI, VS Code, GitHub Copilot ฯลฯ) เนื้อหาของ Skill สั่งให้ agent ตอบคำถามจาก Knowledge Base ที่ token ผูกไว้เท่านั้น ห้ามใช้ web search, web fetch หรือ training data ของตัวเอง เพื่อป้องกันคำตอบที่ผสมแหล่งข้อมูลอื่นโดยผู้ใช้ไม่ทราบ

บันทึกไฟล์ที่ได้ไว้ที่ `SKILL.md` ในโฟลเดอร์ชื่อ `softnix-knowledge` ภายใต้ skills directory ของ agent — สำหรับ Claude Code คือ `.claude/skills/softnix-knowledge/SKILL.md`
