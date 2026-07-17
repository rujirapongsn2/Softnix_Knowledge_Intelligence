# แผนพัฒนา: Temporal & Relationship-aware Legal Retrieval

> เอกสารนี้เป็นแผนปฏิบัติการสำหรับ AI Coder Agent เพื่อยกระดับระบบจาก "Legal Graph (extraction + review)"
> ให้เป็น retrieval ที่เข้าใจ **เวลา (Temporal)**, **ความสัมพันธ์ (Amend/Repeal/Supersede)** และ
> **ลำดับศักดิ์ของกฎหมาย (Authority Hierarchy)** ตามแนวคิด Knowledge Intelligence Layer
>
> เขียนเมื่อ: 2026-07-13 · อ้างอิงโค้ด ณ commit `57de69a` (branch `agent/legal-graph-schema-v2`)

---

## 0. สถานะปัจจุบัน (Gap Analysis)

### สิ่งที่มีอยู่แล้ว (ห้ามทำซ้ำ ให้ต่อยอด)

| ความสามารถ | ตำแหน่งในโค้ด |
|---|---|
| Legal Graph Schema v2 extraction (instrument, provisions, parties, obligations, amendments, references, effective_date) | `apps/api/app/openrouter.py:136` (`extract_legal_metadata`), เก็บใน `documents.legal_metadata` (JSON) |
| Relationship types: `ISSUED_UNDER, IMPLEMENTS, AMENDS, REPEALS, REFERS_TO, GOVERNED_BY` (เป็น ai_suggestion รอ approve) | `apps/api/app/services.py:205` (`LEGAL_GRAPH_RELATIONSHIPS`), `services.py:454` (`build_legal_cross_document_suggestions`) |
| การจับคู่ reference → เอกสารจริงใน KB ด้วย title/number | `apps/api/app/services.py:436` (`_target_instrument`) |
| Graph projection (PostgreSQL entities/relationships + Neo4j) พร้อม review workflow (`origin`, `review_status`, `is_legal`) | `apps/api/app/models.py:101-138`, `graph_store.py`, migration `0011` |
| Retrieval planner (rule-first + LLM fallback) มี intent `legal_provision` | `apps/api/app/planner.py:140,167` |
| Retrieval channels: pgvector, FTS, graph, exact, LightRAG + RRF fusion + optional cross-encoder rerank + trace | `apps/api/app/services.py:1006-1326` |
| Date filter จาก query/request (`published_from/to` บน `documents.published_at`) | `planner.py:105`, `services.py:1082` (`_apply_published_filter`), `schemas.py:80` |
| Per-KB retrieval policy (versioned) | `planner.py:22` (`RetrievalPolicy`), `PATCH /knowledge-bases/{id}/retrieval-config` |

### สิ่งที่ **ยังไม่มี** (คือขอบเขตของแผนนี้)

1. **ไม่มีลำดับศักดิ์ (authority level)** — `document_type` มีแค่ `general/legal/regulation/contract` (`services.py:643`) ไม่มี taxonomy รัฐธรรมนูญ→พ.ร.บ.→กฎกระทรวง→ประกาศ→ระเบียบ→หนังสือเวียน→คู่มือ และไม่ถูกใช้ถ่วงน้ำหนักตอนค้น
2. **ไม่มีสถานะบังคับใช้ (validity/status)** — `effective_date` ถูก extract ลง JSON แต่ไม่เป็นคอลัมน์ query ได้ ไม่มี `effective_to`, ไม่มีสถานะ `in_force/amended/repealed/superseded`
3. **ไม่มี version chain / document family** — กฎหมายเดียวกันหลายฉบับ (แก้ไขเพิ่มเติม) อยู่เป็นเอกสารอิสระ retrieval ดึงทุกฉบับปนกัน
4. **AMENDS/REPEALS ไม่มีผลจริง** — เป็นแค่ edge ให้คน review; ไม่มี logic เปลี่ยนสถานะเอกสาร/มาตราเก่าเมื่อ approve
5. **Fusion เป็น RRF ล้วน** (`services.py:1276`) — ไม่มีน้ำหนัก authority/recency/status; FAQ ชนะ พ.ร.บ. ได้
6. **ไม่มี resolver pipeline ตอน query** — planner เลือกแค่ channel ไม่มีขั้น detect law → resolve current version → resolve amendments
7. **Chunk ไม่รู้จักมาตรา/ข้อ** — `split_text` (`services.py:734`) ตัดตามขนาดตัวอักษร ไม่มี section identity, ไม่มี link ระหว่าง provision entity กับ chunk
8. **ไม่มี conflict detection** — สองเวอร์ชันของมาตราเดียวกันถูกส่งเข้า LLM พร้อมกันโดยไม่มีคำเตือน
9. **LLM ไม่เห็น metadata** — `answer_from_sources` (`openrouter.py:59`) ส่งแค่ title+excerpt; citation ไม่มี version/status

---

## หลักการออกแบบ (บังคับใช้ทุก Phase)

- **PostgreSQL เป็น source of truth** — Neo4j เป็น projection/accelerator เท่านั้น (ตามสถาปัตยกรรมเดิม)
- **Deterministic ก่อน LLM** — ใช้ rule/SQL ก่อน ใช้ LLM เป็น fallback ที่ถูก constrain เสมอ (ตามแนวทาง planner เดิม)
- **Human-in-the-loop** — สถานะทางกฎหมาย (repealed/superseded) ที่ได้จาก AI มีผลต่อ retrieval **ก็ต่อเมื่อ** relationship ผ่าน review (`review_status='verified'`) หรือ admin ตั้งเอง; ค่า manual ชนะค่า extract เสมอ
- **Backward compatible** — KB ที่ไม่มีเอกสารกฎหมายต้องทำงานเหมือนเดิมทุกประการ; feature ใหม่เปิดผ่าน `RetrievalPolicy`
- **ทุกอย่างต้อง trace ได้** — เพิ่ม trace channel ใหม่ผ่าน `_append_retrieval_trace` (`services.py:990`) ห้าม log เนื้อหาเอกสาร/token
- **สไตล์โค้ด** — ตามของเดิม: SQLAlchemy 2.0 typed mappings, Pydantic models ใน `planner.py`/`schemas.py`, test แบบ pytest ใน `apps/api/tests/`

---

## Phase 1 — Legal Registry: ลำดับศักดิ์ + เวลา + เวอร์ชัน (Data Model)

### 1.1 Migration `0014_legal_registry.py`

สร้างตารางใหม่ 3 ตาราง + คอลัมน์ chunk (ทำ pattern เดียวกับ migration `0011` คือรองรับ fresh-install ที่ bootstrap จาก metadata แล้ว):

```
legal_families
  id            String(36) PK
  knowledge_base_id → knowledge_bases.id (index)
  base_title    String(500)        # ชื่อกฎหมายไม่รวม "(ฉบับที่ N) พ.ศ. XXXX"
  normalized_key String(700) index # canonical key สำหรับจับคู่อัตโนมัติ
  created_at/updated_at
  UNIQUE(knowledge_base_id, normalized_key)

legal_instruments
  id            String(36) PK
  knowledge_base_id (index) / document_id → documents.id UNIQUE
  family_id     → legal_families.id NULL (index)
  kind          String(40)   # enum ด้านล่าง
  authority_level Integer    # derive จาก kind, override ได้
  official_title String(500) / official_number String(120) NULL
  issuer        String(300) NULL / jurisdiction String(120) NULL
  version_label String(120) NULL     # เช่น "ฉบับที่ 3 พ.ศ. 2566"
  enacted_year  Integer NULL          # พ.ศ. แปลงเป็น ค.ศ. เก็บ ค.ศ.
  effective_from Date NULL (index) / effective_to Date NULL (index)
  status        String(20) default 'unknown' (index)
                # in_force | amended | superseded | repealed | not_yet_effective | unknown
  status_source String(20) default 'resolver'   # resolver | manual
  status_reason Text NULL              # อธิบายว่า resolver ตัดสินจาก edge ไหน
  review_status String(20) default 'unreviewed'
  created_at/updated_at

legal_instrument_relations
  id String(36) PK
  knowledge_base_id (index)
  source_instrument_id → legal_instruments.id (index)
  target_instrument_id → legal_instruments.id NULL (index)  # NULL = ยัง resolve ไม่ได้
  target_text   String(700) NULL      # ข้อความอ้างอิงดิบ เช่น "ประกาศฯ ฉบับที่ 8"
  target_provision String(120) NULL   # เช่น "ข้อ 12"
  relation      String(30) (index)    # AMENDS|REPEALS|SUPERSEDES|ISSUED_UNDER|IMPLEMENTS|REFERS_TO|GOVERNED_BY
  evidence_quote Text
  confidence    Float NULL
  origin        String(30) default 'legal_schema'   # legal_schema | ai_suggestion | manual
  review_status String(20) default 'suggested' (index)  # suggested | verified | rejected
  created_at/updated_at
  UNIQUE(source_instrument_id, relation, coalesce(target_instrument_id,''), coalesce(target_provision,''))
```

คอลัมน์ใหม่ใน `document_chunks` (สำหรับ Phase 2.1):

```
section_kind   String(30) NULL   # มาตรา|ข้อ|หมวด|ส่วน|บทเฉพาะกาล|preamble
section_number String(60) NULL (index)
section_label  String(200) NULL  # "มาตรา 15 ทวิ"
```

หมายเหตุ: **ไม่ลบ** ตาราง `entities/relationships` — legal graph เดิมยังใช้แสดงผล/review UI; ตารางใหม่เป็น "registry เชิงโครงสร้าง" ที่ SQL join กับ documents/chunks ได้เร็ว และ sync ไป graph เดิมด้วย `_upsert_legal_relationship` ที่มีอยู่

### 1.2 Kind taxonomy + authority level (ไฟล์ใหม่ `apps/api/app/legal_registry.py`)

```python
AUTHORITY_LEVELS = {
    "constitution": 100,          # รัฐธรรมนูญ
    "act": 90,                    # พระราชบัญญัติ / พระราชกำหนด
    "royal_decree": 80,           # พระราชกฤษฎีกา
    "ministerial_regulation": 70, # กฎกระทรวง
    "notification": 60,           # ประกาศ (กระทรวง/กรม)
    "rule": 50,                   # ระเบียบ/ข้อบังคับ
    "circular": 40,               # หนังสือเวียน/หนังสือตอบข้อหารือ
    "guideline": 30,              # แนวปฏิบัติ/คู่มือ/มาตรฐาน
    "resolution": 30,             # มติ
    "contract": 30, "faq": 20, "other": 20,
}
```

ฟังก์ชันที่ต้องมีในไฟล์นี้ (pure function ทั้งหมด ทดสอบได้โดยไม่ต้องมี DB):

- `classify_kind(title: str, extracted_kind: str | None) -> str` — rule-based จากคำขึ้นต้น title ภาษาไทย (`รัฐธรรมนูญ`, `พระราชบัญญัติ/พ.ร.บ.`, `พระราชกำหนด`, `พระราชกฤษฎีกา`, `กฎกระทรวง`, `ประกาศ`, `ระเบียบ`, `ข้อบังคับ`, `หนังสือเวียน`, `แนวปฏิบัติ`, `คู่มือ`, `มติ`) และเทียบค่า extract จาก LLM; ตัดสินไม่ได้ → `other`
- `normalize_family_key(title: str) -> tuple[str, str | None, int | None]` — คืน `(base_title_key, version_label, enacted_year)` โดยตัด pattern `(ฉบับที่ N)`, `พ.ศ. 25XX/พุทธศักราช`, `ฉะบับ`, วรรคตอน/ช่องว่าง; แปลง พ.ศ.→ค.ศ.
- `parse_thai_date(value) -> date | None` — รองรับ "1 มกราคม 2567", ISO, พ.ศ./ค.ศ.
- `parse_provision_refs(text) -> list[dict]` — regex จับ `มาตรา 15 ทวิ/ตรี`, `ข้อ 12`, `หมวด 3`, เลขไทย ๐-๙ (reuse pattern จาก `planner.py:140`)

### 1.3 Upsert registry หลัง legal extraction

ใน `services.py` — จุดที่ job `EXTRACT_LEGAL_METADATA` สำเร็จ (หลัง `sync_legal_document_graph`):

- ฟังก์ชันใหม่ `upsert_legal_instrument(db, document) -> LegalInstrument`
  1. อ่าน `legal_metadata_v2(document)` → instrument block
  2. `classify_kind` + `authority_level`
  3. `normalize_family_key` → หา/สร้าง `legal_families` ใน KB เดียวกัน → ผูก `family_id`
  4. `effective_from` = `parse_thai_date(instrument.effective_date)`; fallback `document.published_at`
  5. เขียน `legal_instrument_relations` จาก `metadata["references"]` และ `metadata["amendments"]` (origin=`legal_schema`, review_status=`suggested`) — resolve `target_instrument_id` ด้วย logic เดียวกับ `_target_instrument` (`services.py:436`) แต่เทียบกับ `legal_instruments` แทน
  6. ห้าม overwrite แถวที่ `status_source='manual'` หรือ field ที่ admin แก้แล้ว (ใช้ pattern เดียวกับ `_upsert_legal_entity` ที่เช็ค `review_status`)
- เรียกจาก `rebuild_legal_graph` (`services.py:498`) ด้วย เพื่อให้ rebuild เติม registry ครบทั้ง KB (idempotent)
- เมื่อ approve/reject relationship ผ่าน API เดิมของ graph review → sync `review_status` มาที่ `legal_instrument_relations` (map ด้วย source/target instrument + relation type)

### 1.4 Status Resolver (deterministic, ไม่ใช้ LLM)

ฟังก์ชัน `resolve_instrument_statuses(db, knowledge_base_id) -> dict[str, int]` ใน `legal_registry.py`:

ลำดับกติกา (ทำงานเฉพาะแถว `status_source='resolver'`):

1. ตั้งต้นทุกตัว = `unknown`; มี `effective_from` และ ≤ วันนี้ → `in_force`; `effective_from` > วันนี้ → `not_yet_effective`
2. Edge `REPEALS` ที่ `review_status='verified'` และมี `target_instrument_id` และ **ไม่มี** `target_provision` → target.status = `repealed`, target.effective_to = source.effective_from (ถ้ายังว่าง)
3. Edge `SUPERSEDES` (verified) → target.status = `superseded`, target.effective_to เช่นเดียวกัน
4. Edge `AMENDS` (verified) → target.status = `amended` (ยังบังคับใช้อยู่แต่มีฉบับแก้ไข) — **ห้าม** ตั้ง effective_to
5. ภายใน family เดียวกัน: เรียงตาม `effective_from`/`enacted_year`; ฉบับล่าสุดที่บังคับใช้แล้ว → `in_force`; ฉบับก่อนหน้า **เฉพาะกรณีที่ title บ่งว่าเป็นฉบับเต็ม (ไม่ใช่ "แก้ไขเพิ่มเติม")** → `superseded` + `status_reason='newer full version in family'`; ถ้าเป็นฉบับแก้ไขเพิ่มเติม (มี "(ฉบับที่ N)") ให้คงสถานะจากกติกา 1-4
6. เขียน `status_reason` ทุกครั้ง (เช่น `repealed by <instrument_id> via verified REPEALS`)

Trigger จุดเรียก: (a) จบ `EXTRACT_LEGAL_METADATA`, (b) จบ `REBUILD_LEGAL_GRAPH` (`services.py:813`), (c) เมื่อ admin approve relationship, (d) endpoint manual `POST /api/v1/knowledge-bases/{kb_id}/legal-registry/resolve`

### 1.5 Extraction prompt update (`openrouter.py:136`)

เพิ่มใน schema/prompt ของ `extract_legal_metadata`:

- `instrument.kind` ต้องเป็นค่าจาก enum: `constitution|act|royal_decree|ministerial_regulation|notification|rule|circular|guideline|resolution|contract|faq|other`
- `instrument.version_label` (เช่น "ฉบับที่ 3"), `instrument.enacted_year_be` (พ.ศ.)
- `instrument.effective_to` (ถ้าระบุวันสิ้นผลในตัวเอกสาร)
- `amendments` แต่ละรายการ: `{action: amends|repeals|supersedes, target_title, target_number, target_provision, evidence_quote}`
- เพิ่ม `SUPERSEDES` ใน relationship enum ของทั้ง `extract_legal_metadata` และ `suggest_legal_relationships` และเพิ่มใน `LEGAL_GRAPH_RELATIONSHIPS` (`services.py:205`)

### 1.6 API (เพิ่มใน `main.py`, schema ใน `schemas.py`)

- `GET /api/v1/knowledge-bases/{kb_id}/legal-registry` — list instruments (filter: status, kind, family_id) + จำนวน relations
- `GET /api/v1/legal-instruments/{id}` — รายละเอียด + version chain ของ family + relations (in/out)
- `PATCH /api/v1/legal-instruments/{id}` — admin override: `kind, authority_level, effective_from, effective_to, status, family_id, version_label` → ตั้ง `status_source='manual'`, `review_status='verified'`, ลง `audit_log` (ใช้ helper `audit.py` เดิม)
- `POST /api/v1/knowledge-bases/{kb_id}/legal-registry/resolve` — รัน status resolver (sync, เร็วพอไม่ต้องเป็น job)

### 1.7 Tests (Phase 1)

ไฟล์ใหม่ `apps/api/tests/test_legal_registry.py`:

- `classify_kind`: อย่างน้อย 12 เคสครอบทุก kind (ไทยเต็ม/ตัวย่อ พ.ร.บ./ภาษาอังกฤษ)
- `normalize_family_key`: "พระราชบัญญัติคุ้มครองแรงงาน พ.ศ. 2541" กับ "พระราชบัญญัติคุ้มครองแรงงาน (ฉบับที่ 7) พ.ศ. 2562" → key เดียวกัน, version ต่างกัน
- `parse_thai_date`: พ.ศ., เลขไทย, ISO, null
- Status resolver truth table: repeal-verified, repeal-suggested (ต้องไม่มีผล), amend, supersede, family ordering, manual ชนะ resolver, `not_yet_effective`
- Idempotency: รัน `upsert_legal_instrument` + `resolve` ซ้ำ 2 ครั้ง ผลเท่าเดิม

**Definition of Done Phase 1:** อัปโหลด พ.ร.บ. 2 เวอร์ชัน + ประกาศยกเลิก → registry มี family เดียว, สถานะถูกต้องหลัง approve edge, ทุก test ผ่าน, `alembic upgrade head` ผ่านทั้ง fresh และ existing DB

---

## Phase 2 — Structure-aware Chunking + Provision Linkage

### 2.1 Section-aware splitter (`services.py:734` บริเวณ `split_text`)

- ฟังก์ชันใหม่ `split_legal_text(text, chunk_size, overlap) -> list[ChunkPiece]` โดย `ChunkPiece = (char_start, char_end, content, section_kind, section_number, section_label)`
  1. สแกนหา heading ด้วย regex: `^(มาตรา|ข้อ)\s*([0-9๐-๙]+(?:/[0-9๐-๙]+)?(?:\s*(ทวิ|ตรี|จัตวา))?)` และ `^(หมวด|ส่วนที่|บทเฉพาะกาล|บทนิยาม)` (multiline, รองรับเลขไทย)
  2. แบ่งเป็น section blocks; block ที่ยาวเกิน `chunk_size` ค่อยตัดต่อด้วย logic เดิมของ `split_text` โดยทุกชิ้นสืบทอด section identity เดิม
  3. เนื้อหาก่อน heading แรก → `section_kind='preamble'`
- ใน `replace_document_chunks` (`services.py:756`): ถ้า `document.document_type in LEGAL_DOCUMENT_TYPES` ใช้ `split_legal_text`, ไม่ใช่ → `split_text` เดิม (ผลลัพธ์ byte-for-byte เท่าเดิมสำหรับเอกสารทั่วไป)
- **สำคัญ:** เอกสารกฎหมายที่ ingest ไปแล้วต้อง re-chunk ได้ — reuse job `REINDEX_EMBEDDINGS`/flow ใน `queue_embedding_reindex` (`services.py:788`) หรือเพิ่ม option `rechunk=true`

### 2.2 Provision ↔ chunk linkage

- หลัง legal extraction: ฟังก์ชัน `link_provisions_to_chunks(db, document)` จับคู่ provision entity (identity `legal:provision:{doc}:{kind}:{number}` — `services.py:403`) กับ chunks ที่ `section_number` ตรงกัน → เก็บ mapping ใน `Entity.attributes["chunk_ids"]`
- ใช้ตอน retrieval: ถ้า resolver รู้ว่าผู้ใช้ถาม "มาตรา 15" → ดึง chunk ตรงมาตราได้โดยตรง (exact provision lookup)

### 2.3 Tests

- `split_legal_text`: เอกสารไทยมี มาตรา/ข้อ/หมวด/เลขไทย/มาตรา 15 ทวิ; ยืนยัน chunk ทุกชิ้นมี section ถูกต้อง; เอกสาร general ไม่เปลี่ยน
- linkage: provision 3 มาตรา ↔ chunks ถูก map ครบ

---

## Phase 3 — Query-time Legal Resolver + Validity-aware Retrieval + Weighted Fusion

> หัวใจของแผน: เปลี่ยนจาก "Search → LLM" เป็น "Resolve → Filter → Weighted Retrieve → Validate → LLM"

### 3.1 ขยาย `RetrievalPolicy` และ `RetrievalPlan` (`planner.py`)

```python
class RetrievalPolicy(BaseModel):
    ...เดิม...
    legal_awareness: bool = True          # master switch ต่อ KB
    exclude_invalid: bool = True          # ตัด repealed/superseded ออกจาก evidence
    authority_weight: float = Field(default=0.30, ge=0, le=1)
    recency_weight: float = Field(default=0.15, ge=0, le=1)
    status_weight: float = Field(default=0.35, ge=0, le=1)

class RetrievalPlan(BaseModel):
    ...เดิม...
    as_of_date: date | None = None
    include_historical: bool = False
    legal_context: LegalContext | None = None

class LegalContext(BaseModel):
    matched_instrument_ids: list[str] = []
    current_version_ids: list[str] = []      # เอกสารที่ "ควรใช้ตอบ"
    amending_instrument_ids: list[str] = []  # ฉบับแก้ไขที่เกี่ยวข้อง
    excluded_document_ids: list[str] = []    # repealed/superseded ที่ต้องตัด
    provision_refs: list[str] = []           # ["มาตรา 15", "ข้อ 12"]
    resolution_notes: list[str] = []         # สำหรับ trace/UI
```

- `QueryFilters` (`schemas.py:80`): เพิ่ม `as_of_date: date | None`, `include_historical: bool = False`; ส่งผ่าน `build_retrieval_plan` (`services.py:962`) แบบเดียวกับ published_from/to
- MCP tool schema + Query Playground ต้องรับ 2 field นี้ (ดู Phase 4)

### 3.2 Legal Resolver (ไฟล์ใหม่ `apps/api/app/legal_resolver.py`)

ฟังก์ชันหลัก `resolve_legal_context(db, query, kb_ids, plan, trace) -> LegalContext | None`

เงื่อนไขเรียก: `policy.legal_awareness` และ KB มีแถวใน `legal_instruments` (เช็คด้วย `EXISTS` เร็ว ๆ) — ไม่เข้าเงื่อนไข → คืน `None` ทุกอย่างทำงานแบบเดิม

ขั้นตอน (deterministic ทั้งหมด, ไม่เรียก LLM ในขั้นนี้):

1. **Detect** — `parse_provision_refs(query)` + จับชื่อกฎหมายในคำถามโดย match กับ `legal_families.base_title` และ `legal_instruments.official_title/official_number` (ILIKE จาก token ยาว ๆ ของ query; ใช้ `normalize_family_key` กับ query ด้วย)
2. **Version resolve** — ต่อ family ที่ match: เลือก instrument ที่ `status='in_force'` หรือ `amended` และ `effective_from <= as_of <= coalesce(effective_to, ∞)` โดย `as_of = plan.as_of_date or today` → `current_version_ids`; ตัวที่ `repealed/superseded` หรืออยู่นอกช่วงเวลา → `excluded_document_ids` (map instrument→document_id)
3. **Amendment expansion** — เดิน `legal_instrument_relations` (verified เท่านั้น) จาก current versions: ฉบับที่ `AMENDS` มัน → `amending_instrument_ids`; ทำ 1 hop พอ
4. **Hierarchy expansion** — เดิน `ISSUED_UNDER`/`IMPLEMENTS` (verified) ขึ้นหา parent act และลงหา subordinate notifications ของ instrument ที่ match → เติมเข้า `current_version_ids` (เฉพาะตัวที่ valid ณ as_of)
5. **Exclusion sweep** — ถ้าไม่ match กฎหมายใดเลยแต่ KB เป็น legal: ยังเติม `excluded_document_ids` จากทุก instrument ที่ invalid ณ as_of (นี่คือกลไกกัน "ข้อ 5 เดิม" โผล่)
6. `include_historical=True` → ไม่เติม `excluded_document_ids` แต่ยังเติม notes/status ให้ evidence
7. เขียน trace: `_append_retrieval_trace(channel="legal_resolver", system="PostgreSQL legal registry", status="used", detail=...)` พร้อมจำนวน matched/excluded

**LLM fallback (จำกัดขอบเขต):** เฉพาะเมื่อ query มี pattern legal (`legal_provision` intent) แต่ match ชื่อกฎหมายไม่ได้และ KB มี >1 family — เพิ่ม method `resolve_instrument_mention(query, candidates)` ใน `openrouter.py` (pattern เดียวกับ `plan_retrieval`: JSON-only, ส่งเฉพาะรายชื่อ candidate ไม่ส่งเนื้อหาเอกสาร, constrain ผลลัพธ์ให้อยู่ใน candidate list) — ทำหลังจาก rule ล้มเหลวเท่านั้น และ timeout ใช้ `retrieval_planner_timeout_seconds`

จุดเชื่อม: เรียกจาก `build_retrieval_plan` (`services.py:962`) หลังได้ `decision` → `decision.plan.legal_context = resolve_legal_context(...)`

### 3.3 Validity filter ในทุก channel (`services.py`)

- helper ใหม่ `_apply_legal_filter(rows, plan)` วางคู่กับ `_apply_published_filter` (`services.py:1082`): ถ้า `plan.legal_context and plan.legal_context.excluded_document_ids` → `rows.filter(Document.id.notin_(excluded))`
- ใช้ใน `query_database_vectors`, `query_database_chunks`, `query_exact_documents`
- **LightRAG channel** filter ที่ DB ไม่ได้ → post-filter ใน `_query_lightrag`/`LightRAGRetrievalEngine.query`: ตัด source ที่ `document_id ∈ excluded` (โค้ด decode document_id จาก label มีแล้ว — `retrieval.py:147-153`) + trace detail ระบุจำนวนที่ถูกตัด
- **Graph channel**: ใน `relationship_sources`/`query_database_graph` ตัด source ที่มาจากเอกสาร excluded เช่นกัน

### 3.4 Metadata-weighted fusion (`fuse_evidence`, `services.py:1276`)

- เปลี่ยน signature เป็น `fuse_evidence(*channels, limit, plan=None, legal_meta=None)` โดย `legal_meta: dict[document_id, {authority_level, status, effective_from}]` โหลดครั้งเดียวด้วย query เดียวจาก `legal_instruments` สำหรับ document_ids ที่โผล่ใน candidates
- สูตร (ยึด RRF เดิมเป็นฐาน ไม่แตะพฤติกรรมเมื่อไม่มี legal_meta):

```
base   = Σ 1/(60+rank)                       # เดิม
boost  = authority_weight * (authority_level/100)
       + status_weight    * status_factor    # in_force=1.0, amended=0.85, not_yet_effective=0.3, unknown=0.5, superseded/repealed=0.1
       + recency_weight   * recency_factor   # 1.0 ถ้า effective_from ล่าสุดใน family, ลดหลั่นตามอันดับ
score  = base * (1 + boost)                  # เอกสารไม่มี legal_meta → boost=0 (พฤติกรรมเดิมเป๊ะ)
```

- **Guaranteed representation:** ถ้า `legal_context.current_version_ids` มี chunk อยู่ใน candidates แต่หลุด top-limit → บังคับแทรกอย่างน้อย 1 chunk ของ current version (กัน FAQ ท่วม)
- Rerank (`rerank_evidence`): จำกัด candidates ให้ผ่าน validity filter แล้วเท่านั้น (ทำอยู่แล้วเพราะ filter เกิดก่อน fusion) และ**คง source ที่ถูกบังคับแทรก**ไว้หลัง rerank

### 3.5 Conflict detection & validation (ฟังก์ชันใหม่ใน `services.py`)

`validate_legal_evidence(db, evidence, plan) -> list[dict]` เรียกใน `query_documents` หลัง rerank ก่อน answer generation:

1. Group sources ตาม `(family_id, section_number)` (join chunks→documents→instruments)
2. พบมากกว่า 1 เวอร์ชันของ provision เดียวกัน → เก็บเฉพาะเวอร์ชันที่ document เป็น current; ตัวอื่นตัดออก (หรือคงไว้ถ้า `include_historical`) + สร้าง warning `{"code": "SUPERSEDED_VERSION_REMOVED", "detail": ...}`
3. Source จากเอกสาร `unknown` status ใน KB ที่มี legal registry → warning `UNVERIFIED_VALIDITY`
4. มี verified `AMENDS` เข้า provision ที่อ้างในคำตอบ แต่ฉบับแก้ไขไม่อยู่ใน evidence → ดึง chunk ของ amending provision (จาก linkage 2.2) เพิ่มเข้า evidence + note
5. คืน warnings → ใส่ `result["warnings"]` (มี field อยู่แล้ว — `services.py:1351`) + trace channel `conflict_check`

### 3.6 Evidence enrichment สำหรับ LLM (`openrouter.py:59`)

- `answer_from_sources(query, sources, legal_notes=None)`: บรรทัด header ของแต่ละ source เปลี่ยนเป็น
  `[S1] พระราชบัญญัติคุ้มครองแรงงาน พ.ศ. 2541 (แก้ไขถึงฉบับที่ 7) | สถานะ: บังคับใช้ | มาตรา 15 | มีผล: 2541-08-19` (สร้าง string ฝั่ง `services.py` แล้วส่งเข้า field ใหม่ของ source เช่น `source["legal_label"]`)
- เพิ่มใน system prompt: *"When sources include legal status metadata: prefer in-force text, state the version and effective date you relied on, and explicitly mention when a provision has been amended or repealed. If two sources conflict, say which one prevails and why (status and authority level)."*
- `sources` payload (ตอบกลับ MCP/UI): เพิ่ม `document_status`, `authority_level`, `kind`, `version_label`, `effective_from`, `effective_to`, `section_label`

### 3.7 Tests (Phase 3)

- `test_legal_resolver.py`: detect ชื่อกฎหมายไทย+มาตรา, version selection ตาม as_of (อดีต/ปัจจุบัน/อนาคต), hierarchy expansion, exclusion sweep, include_historical, ไม่มี registry → คืน None
- `test_retrieval_fusion.py` (ต่อยอดไฟล์เดิม): boost ทำให้ act ชนะ faq ที่ RRF เท่ากัน; ไม่มี legal_meta → คะแนนเท่าสูตรเดิมเป๊ะ; guaranteed representation; superseded ถูกตัด
- Conflict detection: สอง chunk มาตราเดียวกันคนละเวอร์ชัน → เหลือเวอร์ชันเดียว + warning
- E2E (sqlite): fixture 4 เอกสาร — พ.ร.บ.หลัก 2541, ฉบับแก้ไข (ฉบับที่ 7) 2562, ประกาศออกตามความ, FAQ — คำถาม "ค่าชดเชยตามมาตรา 118" ต้องได้ evidence จากฉบับปัจจุบัน + ฉบับแก้ไข, ไม่มีเวอร์ชันเก่า, citations มี status

**Definition of Done Phase 3:** ปัญหาทั้ง 5 ข้อในโจทย์มีกลไกรองรับ: (1) รู้ฉบับใหม่/เก่า/ยกเลิก ผ่าน registry+resolver, (2) ลำดับศักดิ์ถ่วงน้ำหนักใน fusion, (3) เลือก version ตาม as_of, (4) provision ref ถูก resolve ข้ามเอกสาร, (5) override แล้วข้อความเก่าถูกตัด/เตือน

---

## Phase 4 — API, MCP, UI, Observability

### 4.1 API/MCP

- `QueryRequest.filters` รองรับ `as_of_date`, `include_historical` — อัปเดต MCP tool inputSchema (ตำแหน่งประกาศ tool ใน `main.py`, ค้นคำว่า `filters` ใน MCP handler) และ `docs/MCP.md`, `docs/API.md`
- `metadata.retrieval_plan` มี `legal_context` (มีอยู่แล้วอัตโนมัติเมื่อเพิ่มเข้า `RetrievalPlan` เพราะ `model_dump` — `services.py:1352`) — ตรวจว่าไม่ leak เนื้อหาเอกสาร (ใส่เฉพาะ id/note)
- `PATCH /knowledge-bases/{id}/retrieval-config` รับ field ใหม่ของ policy (ผ่าน `RetrievalPolicy` validation อัตโนมัติ)

### 4.2 Web UI (`apps/web/src/main.jsx`)

1. **Document details**: แสดง instrument card — kind (ภาษาไทย), authority level, status badge (สี: เขียว=บังคับใช้, เหลือง=amended, แดง=repealed/superseded, เทา=unknown), effective range, family/version timeline (ลิงก์ไปเอกสารเวอร์ชันอื่น), ปุ่มแก้ไข (PATCH instrument)
2. **Legal registry view** ใน KB: ตาราง instruments filter ตาม status/kind + ปุ่ม "Resolve statuses"
3. **Relationship review**: ตอน approve edge AMENDS/REPEALS/SUPERSEDES แสดง preview ผลกระทบ ("จะทำให้ X กลายเป็นถูกยกเลิก")
4. **Query Playground**: date picker `as_of_date`, toggle "รวมฉบับที่ถูกยกเลิก/แทนที่", แสดง warnings จากผลลัพธ์, badge สถานะบน citation แต่ละรายการ
5. **Trace explorer**: รองรับ channel ใหม่ `legal_resolver`, `conflict_check` (โครงสร้าง trace item เดิมรองรับอยู่แล้ว — เพิ่ม label/สี)

### 4.3 Observability

- `metrics.observe_retrieval` รับ channel ใหม่โดยไม่ต้องแก้ (ตรวจ `observability.py`)
- เพิ่ม counter: จำนวนเอกสารที่ถูก exclude ต่อ query, จำนวน conflict warnings — log แบบ aggregate เท่านั้น

### 4.4 Docs

- อัปเดต `README.md` (ส่วน Legal document extraction), `docs/ARCHITECTURE.md` (เพิ่มแผนภาพ resolver pipeline), `docs/API.md`, `docs/MCP.md`, `docs/ACCEPTANCE.md` (สถานการณ์ทดสอบใหม่ตาม DoD ข้างบน)

---

## Phase 5 — Fixtures & Acceptance (ต้องทำเป็นส่วนหนึ่งของ PR ไม่ใช่ทีหลัง)

สร้าง `fixtures/legal/` (มี dir `fixtures` อยู่แล้ว):

| ไฟล์ | เนื้อหา |
|---|---|
| `act-2541.md` | พ.ร.บ.สมมุติ พ.ศ. 2541 มีมาตรา 1-20 (มาตรา 15 เรื่องค่าชดเชย) |
| `act-amendment-2562.md` | พ.ร.บ.สมมุติ (ฉบับที่ 2) พ.ศ. 2562 — "ให้ยกเลิกความในมาตรา 15 ... และให้ใช้ความต่อไปนี้แทน" |
| `notification-2563.md` | ประกาศกระทรวงออกตามความในมาตรา 6 มีข้อ 1-10 |
| `notification-2566.md` | ประกาศ (ฉบับที่ 2) พ.ศ. 2566 ยกเลิกข้อ 5 ของประกาศ 2563 |
| `faq.md` | FAQ อ้างเนื้อหามาตรา 15 แบบเก่า (เป็นกับดัก similarity) |

Acceptance scenarios (เขียนเป็น integration test + บันทึกใน `docs/ACCEPTANCE.md`):

1. ถาม "มาตรา 15 กำหนดอะไร" → คำตอบอ้างข้อความฉบับแก้ไข 2562, citation แสดงสถานะ, FAQ ไม่ใช่ citation หลัก
2. ถามเดิมด้วย `as_of_date=2560-01-01` → ได้ข้อความฉบับ 2541
3. ถาม "ข้อ 5 ของประกาศ" → ได้ข้อความใหม่จากประกาศ 2566, มี warning ว่าข้อ 5 เดิมถูกยกเลิก
4. เอกสาร general KB เดิม: ผล query และ trace เหมือนก่อนแก้ทุกประการ (regression)
5. Edge ที่ยัง `suggested` (ยังไม่ approve) ต้องไม่มีผลต่อ retrieval

---

## ลำดับการทำงานและขอบเขต PR (สำหรับ Coder Agent)

ทำทีละ Phase ทีละ PR ตามลำดับ 1→2→3→4→5 (Phase 5 fixtures เริ่มเขียนได้ตั้งแต่ Phase 1 เพื่อใช้ test):

1. ทุก PR ต้องรัน `pytest apps/api/tests` ผ่านทั้งหมด และรัน `docker-compose.pgvector-test.yml` สำหรับ test ที่แตะ pgvector
2. ห้ามเปลี่ยนพฤติกรรม default ของ KB ที่ไม่มี legal instruments — ยืนยันด้วย regression test เสมอ
3. Migration ต้องมี downgrade ครบ และรองรับ fresh-install bootstrap (ดู comment ใน `0011_legal_graph_schema_v2.py:26`)
4. ทุก feature ที่กระทบ retrieval ต้องเพิ่ม trace entry — ตรวจใน test ว่า trace มี channel ครบ
5. ห้าม log/persist เนื้อหาเอกสารหรือ token ใน trace/planner logs (นโยบายเดิมของโปรเจกต์)
6. ภาษา UI ใช้แบบแผนเดียวกับหน้าจอเดิมใน `apps/web/src/main.jsx`

## ความเสี่ยงที่ต้องระวัง

- **Title matching ภาษาไทยพลาด** → family ผิด → สถานะผิด: จึงบังคับ human review ก่อนสถานะมีผล (กติกา verified-only) และมี `status_reason` ให้ตรวจย้อนได้
- **Provision-level repeal** (ยกเลิกเฉพาะข้อ ไม่ใช่ทั้งฉบับ): Phase 1 เก็บ `target_provision` ไว้แล้ว; Phase 3 ใช้ระดับ conflict detection/annotation ก่อน — การทำ provision-level status เต็มรูปแบบ (ตาราง `provision_overrides`) เป็นงานต่อยอดหลัง Phase 5
- **เอกสารรวมฉบับแก้ไขแล้ว (consolidated text)** กับฉบับแก้ไขรายฉบับอยู่ปนกัน: ใช้ heuristic ใน status resolver ข้อ 5 + ให้ admin ตั้ง family/version เองได้
- **LightRAG channel** ควบคุมเนื้อหา index ไม่ได้: กันด้วย post-filter ที่ document level เท่านั้น (chunk ภายใน LightRAG อาจ stale — ยอมรับและระบุใน docs)
