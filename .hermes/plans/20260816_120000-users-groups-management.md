# Users & Groups Management Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** เพิ่มระบบ Users Management แบบ 3 role (admin / manager / user) + Groups + per-KB ownership บน Web UI — user จัดการได้เฉพาะ KB ของตัวเอง, manager บริหารเต็มรูปแบบในระดับกลุ่ม, admin บริหารทั้งระบบ พร้อมหน้า Profile ให้ทุกคนเปลี่ยน password ตัวเองได้

**Architecture:** เพิ่ม `role` + `group_id` บน `users`, ตารางใหม่ `groups` และ `kb_owners` (many-to-many), และ `created_by` บน `token_keys` — ส่วน authorization รวมศูนย์ไว้ที่ `security.py` (`require_role()` + `kb_ids_visible_to()` + `assert_kb_access()`) แล้วเสียบทุก endpoint ผ่าน FastAPI dependency เหมือน pattern `current_admin` ที่ใช้อยู่ ไม่เปลี่ยนกลไก cookie/JWT เดิม

**Tech Stack:** FastAPI, SQLAlchemy + Alembic, React (main.jsx) + Kumo design system, argon2, pytest

---

## Current Context

- Production: https://knowledge.softnix.ai (docker compose บนเครื่อง dev), repo = branch `main` @ `7217b0b`
- ผู้ใช้เดียว: `admin` (สร้างจาก `INITIAL_ADMIN_*` ใน `.env`), auth = JWT cookie `skip_access`/`skip_refresh`
- **ทุก protected endpoint (87 ตัว) ใช้ `Depends(current_admin)` = login แล้วได้สิทธิ์เต็ม** — ไม่มี role/group/ownership
- ตาราง `users`: `id, username, password_hash, display_name, is_active` เท่านั้น
- `TokenKey` มี KB scoping แล้ว (`allowed_knowledge_base_ids`, `allowed_ingest_knowledge_base_id`, `allowed_scopes` = mcp|ingest)
- KB list ทั้ง UI และ API ไม่ filter ตาม user
- Migration ล่าสุด = `0026` → ตัวใหม่คือ `0027`
- Tests: `apps/api/tests/` รันด้วย `cd apps/api && python -m pytest -q`
- UI views ปัจจุบัน: knowledge-bases, documents, search, explore, mcp-tokens, ingest-tokens, logs

## Requirements

| # | ข้อกำหนด |
|---|---|
| R1 | จัดการ Knowledge (KB + documents) ผ่าน Web UI |
| R2 | สร้าง/จัดการ MCP Tokens และ Ingest API tokens ผ่าน Web UI |
| R3 | role `user`: เห็น/จัดการเฉพาะ KB ที่ตนเป็นเจ้าของ; ออก token ได้เฉพาะ scope ≤ KB ตนเอง |
| R4 | role `manager`: บริหารเต็มรูปแบบในระดับกลุ่ม (KB ของสมาชิกในกลุ่ม, token ที่ creator อยู่ในกลุ่ม, ดู logs ระดับกลุ่ม) |
| R5 | role `admin`: full system — จัดการ users/groups ทั้งหมด + ทุก KB + ทุก token |
| R6 | users มี groups ได้ (v1: one group per user) |
| R7 | ทุก user login ได้ + เปลี่ยน password ตัวเองที่หน้า Profile |
| R8 | admin จัดการ users ทั้งระบบ (สร้าง/แก้/ปิด/รีเซ็ต password/assign role+group) |

## Permission Matrix

### Knowledge Base / Documents / Search

| Action | user | manager | admin |
|---|---|---|---|
| ดูรายการ KB | เฉพาะ own | ทุก KB ที่มี owner ใน group ตน | ทั้งหมด |
| สร้าง KB | ✅ (ตนเป็น owner; KB ถือเป็นของ group ตน ถ้ามี) | ✅ | ✅ |
| แก้/ลบ/activate/พัฐนา KB | เฉพาะ own | group's | ทั้งหมด |
| Upload/แก้/ลบ/reprocess documents | เฉพาะ own KB | group's | ทั้งหมด |
| ค้นหา (`/query`, `/query/impact`) | เฉพาะ KB ที่เห็น | ตามที่เห็น | ทั้งหมด |

### MCP / Ingest Tokens

| Action | user | manager | admin |
|---|---|---|---|
| ดูรายการ token | ที่ตนเป็น creator | ที่ creator อยู่ใน group ตน | ทั้งหมด |
| สร้าง token | ได้ ถ้า KB scope ⊆ KB ที่ตนเห็น | ได้ ถ้า KB scope ⊆ group ตน | ทุก scope |
| rotate/disable/revoke | ที่ตนเป็น creator | ที่ creator ∈ group ตน | ทั้งหมด |

### Users / Groups / Logs

| Action | user | manager | admin |
|---|---|---|---|
| Users CRUD, reset password, assign role/group | ❌ | ❌ | ✅ |
| Groups CRUD | ❌ | ❌ | ✅ |
| เปลี่ยน password ตนเอง (Profile) | ✅ | ✅ | ✅ |
| Logs/audit/traces | ❌ (ซ่อนเมนู) | เฉพาะ rows ที่ target KB ∈ group ตน | ทั้งหมด |

**หมายเหตุ token scope:** user ออก token ให้ระบบภายนอกได้เฉพาะ KB ของตนเอง — โมเดล machine-to-machine เดิมไม่เปลี่ยน (ยังเป็น Bearer `skik_...`)

## Open Questions + Proposed Defaults

| คำถาม | Default ที่เสนอ |
|---|---|
| KB ผูกกับ group แบบไหน? | ผ่าน ownership derivation: KB ที่มี owner อยู่ใน group X = KB ของ group X (ไม่เพิ่ม kb.group_id column) |
| user ได้หลาย group? | v1 ใช้ `users.group_id` เดี่ยว; ถ้าต้องการ multi-group ค่อยเปลี่ยนเป็น junction table |
| ลบ KB แล้ว token ที่ scoped ไปที่ KB นั้น? | revoke อัตโนมัติทุก token ที่อ้าง KB นั้น (ทั้ง mcp + ingest axis) |
| ลบ group ที่ยังมีสมาชิก? | ปฏิเสธ (409) ให้ย้ายสมาชิกก่อน |
| manager ดู logs? | เฉพาะ audit rows ที่ target KB ∈ group ตน (same-group visibility) |
| Bootstrap? | migration 0027 backfill `role='admin'` ให้ existing `admin` user; `admin` เดิม login ได้ทันที ไม่มี lockout |

---

## Design

### 1) DB — migration `0027_users_groups_rbac.py` (atomic, single migration)

```
groups            (id PK, name UNIQUE, description, created_at, updated_at)
kb_owners         (kb_id FK→knowledge_bases.id, user_id FK→users.id, PK(kb_id,user_id))
users             + role VARCHAR(20) NOT NULL DEFAULT 'user'   -- 'user'|'manager'|'admin'
                  + group_id FK→groups.id NULL
token_keys        + created_by VARCHAR(36) NULL FK→users.id
indexes           ix_kb_owners_user, ix_users_role
backfill          UPDATE users SET role='admin' WHERE username=<INITIAL_ADMIN_USERNAME>
```

Models (`apps/api/app/models.py`): `Group`, `KbOwner` + เพิ่ม fields บน `User`, `TokenKey`

### 2) Authorization — `apps/api/app/security.py`

```python
ROLE_LEVEL = {"user": 0, "manager": 1, "admin": 2}

def require_role(minimum: str):
    def checker(user: User = Depends(current_admin)):
        if ROLE_LEVEL.get(user.role, -1) < ROLE_LEVEL[minimum]:
            error("ROLE_FORBIDDEN", f"Requires '{minimum}' role or above")
        return user
    return checker

require_admin = require_role("admin")  # any authenticated user passes "user"

def kb_ids_visible_to(db: Session, user: User) -> set[str]:
    # admin → ทุก KB ที่ไม่ soft-delete
    # manager → KB ที่มี owner ใน group ตน (รวม KB ตนเอง)
    # user → KB ที่ตนอยู่ใน kb_owners

def assert_kb_access(db: Session, user: User, kb_id: str, *, write: bool = False):
    # admin → ผ่าน
    # manager → kb_id ∈ group's KBs (read+write)
    # user → kb_id ∈ own KBs (read+write — v1 ไม่แยก read/write)
    # else → 404 KNOWLEDGE_BASE_NOT_FOUND (anti-enumeration เหมือน ingest surface)

def token_visible_to(db, user, token) -> bool:
    # admin → True; manager → token.created_by ∈ own group; user → token.created_by == user.id
```

JWT payload เพิ่ม `role` ตอน `create_session_token()` เพื่อให้ frontend รู้สิทธิ์โดยไม่ต้อง fetch ซ้ำ

### 3) Endpoints ใหม่ (`apps/api/app/main.py`)

```
POST   /api/v1/auth/change-password        (ทุก role — ต้องใส่ old password)
GET    /api/v1/users                       (admin)
POST   /api/v1/users                       (admin)
PATCH  /api/v1/users/{id}                  (admin — display_name, role, group_id, is_active)
POST   /api/v1/users/{id}/reset-password   (admin ตั้ง password ใหม่)
GET    /api/v1/groups                      (admin)
POST   /api/v1/groups                      (admin)
PATCH  /api/v1/groups/{id}                 (admin)
DELETE /api/v1/groups/{id}                 (admin — 409 ถ้ามีสมาชิก/มี KB)
GET    /api/v1/auth/me                     (ขยาย: คืน role, group ด้วย)
```

### 4) Endpoints เดิมที่ต้องเสียบ scoping

- `GET /knowledge-bases` → filter ด้วย `kb_ids_visible_to`
- `POST /knowledge-bases` → ใส่ `current_user` เป็น owner ใน `kb_owners`
- `PATCH/DELETE /knowledge-bases/{id}`, `/activate`, document CRUD, upload, reprocess, reindex, legal-extract/metadata, graph sync, `/query`, `/query/impact` → `assert_kb_access`
- `GET/POST/PATCH/DELETE /tokens/*` → list ตาม `token_visible_to`; create ตรวจ KB scope ⊆ visible; rotate/disable/revoke ตรวน `token_visible_to`
- `GET /audit-logs`, `/logs/transactions`, `/traces*` → `require_admin` + manager filter ตาม group KBs (user role ไม่มีสิทธิ์)
- ingest surface (`/api/v1/ingest/*`) ไม่แตะ — ใช้ Bearer token อยู่แล้ว

### 5) Frontend (`apps/web/src/main.jsx` + `translations.js` + `ui.jsx`)

- sidebar เพิ่ม: **Users** (admin), **Groups** (admin), **Profile** (ทุกคน) — ใช้ component เดิม (Card, TextInput, DesignSystemSelect, Button)
- `auth/me` คืน `role` → เก็บใน state ของ App; `WORKSPACE_VIEWS` เพิ่ม `users`, `groups`, `profile`; sidebar/logs แสดงตาม role
- Users view: ตาราง users + form สร้าง/แก้ (username, display_name, role select, group select, is_active) + reset password dialog
- Groups view: ตาราง groups + จำนวนสมาชิก + form สร้าง/แก้/ลบ
- Profile view: แสดง username/role/group + form เปลี่ยน password (old + new + confirm)
- KB list: แสดง badge "ของฉัน"/"กลุ่ม" ไม่จำเป็นใน v1 — ทำเฉพาะ filter ที่ API ส่งมา (YAGNI)
- tokens view: ซ่อน/บล็อกการเลือก KB ที่เกินสิทธิ์ (options มาจาก KB list ที่ถูก filter แล้วอยู่แล้ว)
- translations: เพิ่ม keys ทั้ง th + en

---

## Tasks

> ทุก task จบด้วย commit; รัน test ใน `apps/api` ด้วย `python -m pytest -q`
> ทำงานบน branch `feat/users-groups-rbac` แตกจาก `main`

### Task 1: Models + migration 0027

**Files:** Modify `apps/api/app/models.py`; Create `apps/api/migrations/versions/0027_users_groups_rbac.py`

- เพิ่ม `class Group`, `class KbOwner`; เพิ่ม `role`, `group_id` บน `User`; `created_by` บน `TokenKey`
- Alembic migration ตาม DDL ใน Design §1 + backfill role admin
- ตรวจ: `docker compose run --rm api alembic upgrade head` บน local dev แล้ว `\d users` เห็น columns ใหม่

**Commit:** `feat(db): add groups, kb_owners, users.role/group_id, token_keys.created_by`

### Task 2: security helpers (TDD)

**Files:** Modify `apps/api/app/security.py`; Create `apps/api/tests/test_rbac.py`

- เขียน tests ก่อน: `require_role` ปฏิเสธ role ต่ำกว่า, `kb_ids_visible_to` คืน set ถูกทั้ง 3 role, `assert_kb_access` โยน 404 เมื่อไม่มีสิทธิ์
- แล้ว implement `ROLE_LEVEL`, `require_role()`, `kb_ids_visible_to()`, `assert_kb_access()`, `token_visible_to()`
- `create_session_token()` เพิ่ม `role` ใน payload
- ตรวจ: `python -m pytest tests/test_rbac.py -q` — PASS

**Commit:** `feat(security): role hierarchy + KB visibility helpers`

### Task 3: change-password + /auth/me ขยาย (TDD)

**Files:** Modify `apps/api/app/main.py`, `apps/api/app/schemas.py`; Test `tests/test_rbac.py`

- `POST /auth/change-password` — ตรวจ old password (argon2 `verify`), set hash ใหม่, audit `auth.password_changed`
- `GET /auth/me` คืน `{id, username, role, group}`
- Tests: เปลี่ยน password ผิด/ถูก, me คืน role
- ตรวจ: `python -m pytest tests/test_rbac.py -q`

**Commit:** `feat(auth): self-service password change + role in /auth/me`

### Task 4: Users CRUD (admin) (TDD)

**Files:** Modify `apps/api/app/main.py`, `apps/api/app/schemas.py`; Test `tests/test_rbac.py`

- `GET/POST /users`, `PATCH /users/{id}`, `POST /users/{id}/reset-password` — ทุกตัว `Depends(require_admin)`
- กัน deadmin ตัวเอง: PATCH ตนเองเปลี่ยน role จาก admin → อื่น หรือ `is_active=false` ตัวเอง = 409 `LAST_ADMIN_GUARD`
- สร้าง user: username unique (409), password ≥ 8 chars (422), role ∈ {user,manager,admin}
- Tests: admin สร้าง/แก้/reset ได้, manager/user โดน 403, self-demote guard
- ตรวจ: `python -m pytest tests/test_rbac.py -q`

**Commit:** `feat(api): admin users CRUD + password reset`

### Task 5: Groups CRUD (admin) (TDD)

**Files:** เดียวกับ Task 4; Test `tests/test_rbac.py`

- `GET/POST /groups`, `PATCH/DELETE /groups/{id}` — `require_admin`
- DELETE: 409 ถ้ามี user อ้างอิง (`users.group_id`) หรือมี KB ใน group
- Tests: CRUD ผ่าน, delete มีสมาชิก = 409, non-admin = 403
- ตรวจ: `python -m pytest tests/test_rbac.py -q`

**Commit:** `feat(api): admin groups CRUD`

### Task 6: KB scoping (TDD)

**Files:** Modify `apps/api/app/main.py` (KB list/create/update/delete/activate + document/query endpoints); Test `tests/test_rbac.py`

- list → filter; create → insert `kb_owners`; ทุก KB-scoped action → `assert_kb_access`
- ลบ KB → revoke token ทุกตัวที่อ้าง KB นั้น (default จาก Open Questions)
- Tests: user A ไม่เห็น/แตะไม่ได้ KB ของ user B (404), manager เห็น KB ของสมาชิก group, admin เห็นทุกอัน, create แล้ว own, delete KB → token revoked
- ตรวจ: `python -m pytest tests/test_rbac.py -q && python -m pytest -q` (regression ทั้งชุด)

**Commit:** `feat(api): per-KB visibility + ownership enforcement`

### Task 7: Token scoping (TDD)

**Files:** Modify `apps/api/app/main.py` (tokens endpoints); Test `tests/test_rbac.py`

- list → `token_visible_to`; create → ตรวจ KB scope ⊆ `kb_ids_visible_to` + บันทึก `created_by`; rotate/disable/revoke → `token_visible_to`
- stamp `created_by` ให้ token เดิมที่ยัง NULL = admin bootstrap
- Tests: user ออก token เกินสิทธิ์ = 403, manager จัดการ token ของ group ได้, user ไม่แตะ token คนอื่น (404)
- ตรวจ: `python -m pytest tests/test_rbac.py -q`

**Commit:** `feat(api): token creator scoping`

### Task 8: Logs scoping

**Files:** Modify `apps/api/app/main.py` (audit-logs, logs/transactions, traces)

- `require_admin` สำหรับทั้งสาม + manager ผ่าน `require_role("manager")` พร้อม filter rows ที่ target KB ∈ group KBs; user = 403
- ตรวจ: `python -m pytest -q`

**Commit:** `feat(api): role-gated logs with group filtering`

### Task 9: Frontend — Users + Groups views (admin)

**Files:** Modify `apps/web/src/main.jsx`, `apps/web/src/translations.js`, `apps/web/src/access.css`

- `WORKSPACE_VIEWS` += `users`, `groups`; sidebar category "Administration" เพิ่ม 2 items (icon: Users, UsersThree)
- Users view: ตาราง (username, display_name, role, group, status) + drawer สร้าง/แก้ + reset-password modal — reuse pattern จาก Documents/MCP tokens view
- Groups view: ตาราง + form
- `/auth/me` เก็บ `role` + `group` ใน App state; ซ่อนเมนูตาม role (logs เฉพาะ manager+, users/groups เฉพาะ admin)
- translations th/en ครบ
- ตรวจ: `cd apps/web && npm run build` ผ่าน + ทดสอบบน localhost:8081

**Commit:** `feat(web): admin users & groups management views`

### Task 10: Frontend — Profile view (ทุก role)

**Files:** Modify `apps/web/src/main.jsx`, `translations.js`

- sidebar ล่าง (ใกล้ language toggle): ชื่อ user → คลิกเข้า Profile
- Profile view: แสดง username, display_name, role, group + form เปลี่ยน password (old/new/confirm; เรียก `/auth/change-password`; สำเร็จ → notify + ล้าง form)
- ตรวจ: build + manual test เปลี่ยน password จริงบน local

**Commit:** `feat(web): profile page with self-service password change`

### Task 11: Regression ทั้งชุด + E2E บน local docker

- `cd apps/api && python -m pytest -q` — ทุก test เดิม + ใหม่ PASS
- local docker: `docker compose up -d --build` → ทดสอบ matrix จริงผ่าน curl:
  - admin login → สร้าง group "Legal" + user "somchai" (manager) + user "nee" (user, group Legal)
  - nee login → สร้าง KB → เห็นแค่ KB ตัวเอง
  - somchai login → เห็น KB ของ nee (same group), จัดการได้, ไม่เห็น users menu (API 403)
  - admin เห็นทุกอย่าง
  - nee เปลี่ยน password ตัวเอง → login ด้วย password ใหม่ได้
  - nee ออก ingest token ได้เฉพาะ KB ตัวเอง

**Commit:** `test(e2e): rbac matrix verification`

### Task 12: Deploy production + ตรวจจริง

- `docker compose -f docker-compose.production.yml up -d --build` (ตาม flow deploy ของ repo)
- ตรวจ https://knowledge.softnix.ai: admin เดิม login ได้ (role=admin จาก backfill), `/auth/me` คืน role
- ทดสอบ matrix ย่อ (สร้าง test group/user → ตรวจ visibility → ลบทิ้ง)
- รายงาน PASS/PARTIAL/FAIL

**Commit:** `docs: rbac rollout notes`

---

## Verification (สรุป)

1. `python -m pytest -q` ใน `apps/api` — ทุก suite ผ่าน (รวม test เดิม 560+ บรรทัด)
2. Migration รันสะอาดทั้ง local + production; `admin` เดิมเข้าได้ ไม่มี lockout
3. E2E matrix 3 role × (KB visibility, token scoping, logs, profile) บน production URL
4. `npm run build` ผ่าน; UI ซ่อนเมนูตาม role

## Risks / Trade-offs

- **87 endpoints ต้องเสียบ scoping** — ความเสี่ยงพลาดจุดไหนจุดหนึ่ง; ลดด้วยการ enforce ที่ dependency + regression tests ทั้งชุด + ทดสอบ matrix จริง
- **existing tokens ไม่มี `created_by`** — backfill เป็น admin; ผลข้างเคียง: manager/user เก่าไม่เห็น token เดิม (ยอมรับได้ เพราะก่อนหน้านี้มี admin คนเดียว)
- **JWT เก่าไม่มี role** — ให้ `current_admin` decode แล้วอ่าน role จาก DB ตอน auth (ไม่ไว้ใจ JWT เพียงอย่างเดียว) — ปลอดภัยกว่า และไม่บังคับ logout ทุกคน
- **ingest surface ไม่แตะ** — Bearer token flow เดิมทำงานเหมือนเดิม ไม่มี regression กับ InsightDOC integration
- **one group per user (v1)** — ถ้าต้องการ multi-group ต้อง migration เพิ่ม junction table ภายหลัง
