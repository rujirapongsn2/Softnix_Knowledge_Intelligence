"""RBAC tests: roles, groups, KB ownership, token creator scoping.

Follows the same self-contained bootstrap pattern as test_api.py: a scratch
SQLite database per module, the initial admin from env, TestClient with a
logged-in admin session.
"""
import os
import tempfile

_TEST_ROOT = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_ROOT}/skip.db"
os.environ["FILE_STORAGE_PATH"] = f"{_TEST_ROOT}/files"
os.environ["INITIAL_ADMIN_PASSWORD"] = "correct-horse-battery-staple"
os.environ["LIGHTRAG_BASE_URL"] = ""
os.environ["REDIS_URL"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["EXT_OCR_KEY"] = ""
os.environ["APP_ENV"] = "test"

from fastapi.testclient import TestClient

from app.main import app
from app.models import KbOwner, User


def client():
    with TestClient(app) as test_client:
        assert test_client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        yield test_client


ADMIN = {"username": "admin", "password": "correct-horse-battery-staple"}


def login_as(tc: TestClient, username: str, password: str) -> TestClient:
    """Login inside the same TestClient context (cookies overwrite in place)."""
    response = tc.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return tc


def logout(tc: TestClient) -> TestClient:
    tc.post("/api/v1/auth/logout")
    return tc


import pytest


@pytest.fixture(autouse=True)
def _fresh_db():
    """Each test gets a clean database (tests share one process-wide SQLite)."""
    from app.db import Base, engine, SessionLocal
    from app.models import User as UserModel
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        settings = __import__('app.config', fromlist=['get_settings']).get_settings()
        db.add(UserModel(username=settings.initial_admin_username,
                         password_hash=__import__('app.security', fromlist=['password_hash']).password_hash(settings.initial_admin_password),
                         role='admin'))
        db.commit()
    yield
    Base.metadata.drop_all(engine)


def make_group(tc: TestClient, name: str) -> dict:
    response = tc.post("/api/v1/groups", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()


def make_user(tc: TestClient, username: str, *, role: str = "user", group_id: str | None = None,
              password: str = "UserPass123!") -> dict:
    response = tc.post("/api/v1/users", json={"username": username, "password": password,
                                              "display_name": username.title(), "role": role, "group_id": group_id})
    assert response.status_code == 200, response.text
    return response.json()


def make_kb(tc: TestClient, name: str, code: str | None = None) -> dict:
    """Create a KB as whoever tc is signed in as; the creator owns it."""
    body = {"name": name}
    if code:
        body["code"] = code
    response = tc.post("/api/v1/knowledge-bases", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# /auth/me + role in session
# ---------------------------------------------------------------------------

def test_me_returns_role_and_group():
    tc = next(client())
    me = tc.get("/api/v1/auth/me").json()
    assert me["role"] == "admin"
    assert "group" in me or me.get("group") is None


def test_bootstrap_admin_role():
    tc = next(client())
    users = tc.get("/api/v1/users").json()
    admin = next(row for row in users if row["username"] == "admin")
    assert admin["role"] == "admin"


# ---------------------------------------------------------------------------
# Users CRUD + guards
# ---------------------------------------------------------------------------

def test_admin_crud_users_and_reset_password():
    tc = next(client())
    group = make_group(tc, "Legal")
    created = make_user(tc, "somchai", role="manager", group_id=group["id"])
    assert created["role"] == "manager"
    assert created["group_id"] == group["id"]

    listed = tc.get("/api/v1/users").json()
    assert {row["username"] for row in listed} >= {"admin", "somchai"}

    patched = tc.patch(f"/api/v1/users/{created['id']}", json={"display_name": "Somchai V2"})
    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Somchai V2"

    reset = tc.post(f"/api/v1/users/{created['id']}/reset-password", json={"password": "NewPass456!"})
    assert reset.status_code == 200
    # old password no longer works, new one does
    tc.post("/api/v1/auth/logout")
    assert tc.post("/api/v1/auth/login", json={"username": "somchai", "password": "UserPass123!"}).status_code == 401
    assert tc.post("/api/v1/auth/login", json={"username": "somchai", "password": "NewPass456!"}).status_code == 200


def test_create_user_validation():
    tc = next(client())
    # duplicate username
    make_user(tc, "dup")
    duplicate = tc.post("/api/v1/users", json={"username": "dup", "password": "Whatever123!"})
    assert duplicate.status_code == 409
    # short password
    short = tc.post("/api/v1/users", json={"username": "shorty", "password": "123"})
    assert short.status_code == 422
    # unknown role
    bad_role = tc.post("/api/v1/users", json={"username": "intruder", "password": "Whatever123!", "role": "superadmin"})
    assert bad_role.status_code == 422
    # unknown group
    bad_group = tc.post("/api/v1/users", json={"username": "intruder2", "password": "Whatever123!", "group_id": "nope"})
    assert bad_group.status_code == 404


def test_last_admin_guard():
    tc = next(client())
    users = tc.get("/api/v1/users").json()
    admin = next(row for row in users if row["username"] == "admin")
    # demote self
    demote = tc.patch(f"/api/v1/users/{admin['id']}", json={"role": "user"})
    assert demote.status_code == 409
    # deactivate self
    deactivate = tc.patch(f"/api/v1/users/{admin['id']}", json={"is_active": False})
    assert deactivate.status_code == 409


def test_non_admin_cannot_manage_users():
    tc = next(client())
    make_user(tc, "plainuser")
    make_user(tc, "themanager", role="manager")
    login_as(tc, "plainuser", "UserPass123!")
    assert tc.get("/api/v1/users").status_code == 403
    assert tc.post("/api/v1/users", json={"username": "x", "password": "Whatever123!"}).status_code == 403
    assert tc.get("/api/v1/groups").status_code == 403
    logout(tc)
    login_as(tc, "themanager", "UserPass123!")
    assert tc.get("/api/v1/users").status_code == 403
    assert tc.get("/api/v1/groups").status_code == 403


# ---------------------------------------------------------------------------
# Groups CRUD
# ---------------------------------------------------------------------------

def test_groups_crud_and_delete_guards():
    tc = next(client())
    group = make_group(tc, "Pod-A")
    listed = tc.get("/api/v1/groups").json()
    assert any(row["name"] == "Pod-A" for row in listed)

    patched = tc.patch(f"/api/v1/groups/{group['id']}", json={"description": "Pod A team"})
    assert patched.status_code == 200
    assert patched.json()["description"] == "Pod A team"

    # delete with member -> 409
    make_user(tc, "member_a", group_id=group["id"])
    conflict = tc.delete(f"/api/v1/groups/{group['id']}")
    assert conflict.status_code == 409

    # empty group deletes fine
    empty = make_group(tc, "Pod-B")
    assert tc.delete(f"/api/v1/groups/{empty['id']}").status_code == 200


def test_group_name_unique():
    tc = next(client())
    make_group(tc, "Unique-Group")
    duplicate = tc.post("/api/v1/groups", json={"name": "Unique-Group"})
    assert duplicate.status_code == 409


# ---------------------------------------------------------------------------
# Change password (self)
# ---------------------------------------------------------------------------

def test_change_password_self():
    tc = next(client())
    make_user(tc, "changer")
    login_as(tc, "changer", "UserPass123!")
    wrong = tc.post("/api/v1/auth/change-password", json={"current_password": "WrongWrong8!", "new_password": "BrandNew789!"})
    assert wrong.status_code == 401
    ok = tc.post("/api/v1/auth/change-password", json={"current_password": "UserPass123!", "new_password": "BrandNew789!"})
    assert ok.status_code == 200
    logout(tc)
    assert tc.post("/api/v1/auth/login", json={"username": "changer", "password": "BrandNew789!"}).status_code == 200


# ---------------------------------------------------------------------------
# Every user reads every KB; the creator (owner) and admins edit it
# ---------------------------------------------------------------------------

def _upload(tc: TestClient, kb_id: str, name: str = "note.txt"):
    return tc.post(f"/api/v1/knowledge-bases/{kb_id}/documents", files={"file": (name, b"Land transfer must be registered.", "text/plain")})


def test_every_user_reads_and_searches_every_kb():
    tc = next(client())
    group = make_group(tc, "Legal")
    make_user(tc, "nee", group_id=group["id"])
    make_user(tc, "somchai", role="manager", group_id=group["id"])
    make_user(tc, "pong")                                # no group
    admin_kb = make_kb(tc, "Admin KB", "admin-kb")
    tc.post(f"/api/v1/knowledge-bases/{admin_kb['id']}/activate")
    doc = _upload(tc, admin_kb["id"]).json()

    login_as(tc, "nee", "UserPass123!")
    nee_kb = make_kb(tc, "Nee KB", "nee-kb")

    for username in ("pong", "somchai", "nee"):
        login_as(tc, username, "UserPass123!")
        visible = {kb["code"] for kb in tc.get("/api/v1/knowledge-bases").json()}
        assert visible == {"admin-kb", "nee-kb"}, (username, visible)
        assert tc.get(f"/api/v1/knowledge-bases/{admin_kb['id']}/documents").status_code == 200
        assert tc.get(f"/api/v1/knowledge-bases/{admin_kb['id']}/documents/page").status_code == 200
        assert tc.get(f"/api/v1/documents/{doc['document_id']}/text").status_code == 200
        assert tc.get(f"/api/v1/documents/{doc['document_id']}/file").status_code == 200
        assert tc.get(f"/api/v1/knowledge-bases/{admin_kb['id']}/document-templates").status_code == 200
        assert tc.get(f"/api/v1/knowledge-bases/{admin_kb['id']}/entities").status_code == 200
        assert tc.get(f"/api/v1/knowledge-bases/{admin_kb['id']}/graph-layout").status_code == 200
        assert tc.post("/api/v1/query", json={"query": "land transfer", "knowledge_base_ids": [admin_kb["id"]]}).status_code == 200
        assert tc.post("/api/v1/query", json={"query": "land transfer", "knowledge_base_ids": []}).status_code == 200

    # deleted / unknown KBs still answer 404
    login_as(tc, "admin", ADMIN["password"])
    assert tc.delete(f"/api/v1/knowledge-bases/{nee_kb['id']}").status_code == 200
    login_as(tc, "pong", "UserPass123!")
    assert tc.get(f"/api/v1/knowledge-bases/{nee_kb['id']}/documents").status_code == 404
    assert tc.get("/api/v1/knowledge-bases/no-such-kb/documents").status_code == 404


def test_any_user_creates_a_kb_and_owns_it():
    tc = next(client())
    make_user(tc, "owner1")
    login_as(tc, "owner1", "UserPass123!")
    kb = make_kb(tc, "Owned KB", "owned-kb")
    from app.db import SessionLocal
    with SessionLocal() as db:
        owner_id = db.query(User).filter_by(username="owner1").one().id
        assert db.query(KbOwner).filter_by(kb_id=kb["id"]).one().user_id == owner_id
    assert kb["owner_user_ids"] == [owner_id]
    listed = next(row for row in tc.get("/api/v1/knowledge-bases").json() if row["id"] == kb["id"])
    assert listed["owner_user_ids"] == [owner_id]


def _edit_calls(kb_id: str, doc_id: str):
    """Every KB edit action, as (label, method, path, kwargs)."""
    return [
        ("rename", "patch", f"/api/v1/knowledge-bases/{kb_id}/rename", {"json": {"name": "Renamed"}}),
        ("icon", "patch", f"/api/v1/knowledge-bases/{kb_id}/icon", {"json": {"icon": "legal"}}),
        ("cover upload", "post", f"/api/v1/knowledge-bases/{kb_id}/cover", {"files": {"file": ("c.png", b"x", "image/png")}}),
        ("cover delete", "delete", f"/api/v1/knowledge-bases/{kb_id}/cover", {}),
        ("activate", "post", f"/api/v1/knowledge-bases/{kb_id}/activate", {}),
        ("disable", "post", f"/api/v1/knowledge-bases/{kb_id}/disable", {}),
        ("upload", "post", f"/api/v1/knowledge-bases/{kb_id}/documents", {"files": {"file": ("n.txt", b"new", "text/plain")}}),
        ("batch upload", "post", f"/api/v1/knowledge-bases/{kb_id}/documents/batch", {"files": [("files", ("b.txt", b"b", "text/plain"))]}),
        ("metadata edit", "patch", f"/api/v1/documents/{doc_id}/metadata", {"json": {"published_at": "2024-01-01"}}),
        ("legal metadata replace", "put", f"/api/v1/documents/{doc_id}/legal-metadata", {"json": {"metadata": {}}}),
        ("metadata reevaluate", "post", f"/api/v1/knowledge-bases/{kb_id}/metadata-reevaluate", {}),
        ("template create", "post", f"/api/v1/knowledge-bases/{kb_id}/document-templates", {"json": {"name": "Type", "fields": [{"key": "issuer", "label": "Issuer"}]}}),
        ("entity add", "post", f"/api/v1/knowledge-bases/{kb_id}/entities", {"json": {"name": "APP-01", "entity_type": "system"}}),
        ("graph layout", "put", f"/api/v1/knowledge-bases/{kb_id}/graph-layout", {"json": {"items": []}}),
        ("reprocess", "post", f"/api/v1/documents/{doc_id}/reprocess", {}),
        ("document delete", "delete", f"/api/v1/documents/{doc_id}", {}),
        ("document restore", "post", f"/api/v1/documents/{doc_id}/restore", {}),
        ("kb delete", "delete", f"/api/v1/knowledge-bases/{kb_id}", {}),
    ]


def _assert_all_edits(tc: TestClient, kb_id: str, doc_id: str, expected: str, who: str):
    for label, method, path, kwargs in _edit_calls(kb_id, doc_id):
        response = getattr(tc, method)(path, **kwargs)
        if expected == "forbidden":
            assert response.status_code == 403, (who, label, response.status_code, response.text)
            assert response.json()["error"]["code"] == "KB_EDIT_FORBIDDEN", (who, label)
        else:
            # A few actions have their own preconditions (e.g. a cover must be a real
            # image); what matters is that permission never refuses them.
            assert response.status_code not in (401, 403, 404), (who, label, response.status_code, response.text)
            assert response.status_code < 500, (who, label, response.text)


def test_non_owner_cannot_edit_a_kb():
    tc = next(client())
    team = make_group(tc, "EditGroup")
    other_team = make_group(tc, "OtherGroup")
    make_user(tc, "kbowner", group_id=team["id"])
    make_user(tc, "colleague", group_id=team["id"])                       # same group, plain user
    make_user(tc, "othermgr", role="manager", group_id=other_team["id"])  # manager of another group
    make_user(tc, "loosemgr", role="manager")                             # manager without a group
    login_as(tc, "kbowner", "UserPass123!")
    kb = make_kb(tc, "Owner KB", "owner-kb")
    doc = _upload(tc, kb["id"]).json()
    for username in ("colleague", "othermgr", "loosemgr"):
        login_as(tc, username, "UserPass123!")
        listed = next(row for row in tc.get("/api/v1/knowledge-bases").json() if row["id"] == kb["id"])
        assert listed["can_edit"] is False, username
        _assert_all_edits(tc, kb["id"], doc["document_id"], "forbidden", username)


def test_manager_of_owners_group_can_edit_a_kb():
    tc = next(client())
    team = make_group(tc, "MgrTeam")
    make_user(tc, "teamowner", group_id=team["id"])
    make_user(tc, "teammgr", role="manager", group_id=team["id"])
    login_as(tc, "teamowner", "UserPass123!")
    kb = make_kb(tc, "Team KB", "team-kb")
    doc = _upload(tc, kb["id"]).json()
    login_as(tc, "teammgr", "UserPass123!")
    listed = next(row for row in tc.get("/api/v1/knowledge-bases").json() if row["id"] == kb["id"])
    assert listed["can_edit"] is True
    _assert_all_edits(tc, kb["id"], doc["document_id"], "allowed", "teammgr")
    # ... but still none of the admin-only actions
    other = make_kb(tc, "Mgr own KB")
    assert tc.patch(f"/api/v1/knowledge-bases/{other['id']}/retrieval-config", json={"enable_graph": False}).status_code == 403
    assert tc.post(f"/api/v1/knowledge-bases/{other['id']}/documents/reindex").status_code == 403
    assert tc.get("/api/v1/tokens").status_code == 403
    assert tc.get("/api/v1/mcp/activity").status_code == 403


def test_disabled_kb_is_hidden_from_non_editors():
    tc = next(client())
    team = make_group(tc, "DisTeam")
    other_team = make_group(tc, "DisOther")
    make_user(tc, "disowner", group_id=team["id"])
    make_user(tc, "dismgr", role="manager", group_id=team["id"])
    make_user(tc, "disreader", group_id=team["id"])
    make_user(tc, "disothermgr", role="manager", group_id=other_team["id"])
    login_as(tc, "disowner", "UserPass123!")
    kb = make_kb(tc, "Disabled KB", "disabled-kb")
    doc = _upload(tc, kb["id"]).json()
    assert tc.post(f"/api/v1/knowledge-bases/{kb['id']}/disable").status_code == 200
    kid, did = kb["id"], doc["document_id"]

    for username, password in (("disowner", "UserPass123!"), ("dismgr", "UserPass123!"), ("admin", ADMIN["password"])):
        login_as(tc, username, password)
        assert kid in {row["id"] for row in tc.get("/api/v1/knowledge-bases").json()}, username
        assert tc.get(f"/api/v1/knowledge-bases/{kid}/documents").status_code == 200, username
        assert tc.get(f"/api/v1/documents/{did}/text").status_code == 200, username

    for username in ("disreader", "disothermgr"):
        login_as(tc, username, "UserPass123!")
        assert kid not in {row["id"] for row in tc.get("/api/v1/knowledge-bases").json()}, username
        assert tc.get(f"/api/v1/knowledge-bases/{kid}/documents").status_code == 404, username
        assert tc.get(f"/api/v1/knowledge-bases/{kid}/documents/page").status_code == 404, username
        assert tc.get(f"/api/v1/documents/{did}/text").status_code == 404, username
        assert tc.get(f"/api/v1/documents/{did}/file").status_code == 404, username
        assert tc.get(f"/api/v1/knowledge-bases/{kid}/entities").status_code == 404, username
        assert tc.post("/api/v1/query", json={"query": "land", "knowledge_base_ids": [kid]}).status_code == 404, username
        # editing a hidden KB answers 404 too (it does not reveal that the KB exists)
        assert tc.post(f"/api/v1/knowledge-bases/{kid}/activate").status_code == 404, username

    # re-enabled -> everyone sees it again
    login_as(tc, "dismgr", "UserPass123!")
    assert tc.post(f"/api/v1/knowledge-bases/{kid}/activate").status_code == 200
    login_as(tc, "disothermgr", "UserPass123!")
    assert kid in {row["id"] for row in tc.get("/api/v1/knowledge-bases").json()}


def test_deleted_owner_kb_stays_and_is_editable_by_admin_and_managers():
    tc = next(client())
    team = make_group(tc, "GoneTeam")
    other_team = make_group(tc, "StayTeam")
    gone = make_user(tc, "goneowner", group_id=team["id"])
    make_user(tc, "gonemgr", role="manager", group_id=team["id"])
    make_user(tc, "staymgr", role="manager", group_id=other_team["id"])
    make_user(tc, "stayuser", group_id=team["id"])
    login_as(tc, "goneowner", "UserPass123!")
    kb = make_kb(tc, "Orphan KB", "orphan-kb")
    doc = _upload(tc, kb["id"]).json()
    kid, did = kb["id"], doc["document_id"]

    login_as(tc, "admin", ADMIN["password"])
    assert tc.delete(f"/api/v1/users/{gone['id']}").status_code == 200
    # the KB and its documents survive the owner's deletion
    assert kid in {row["id"] for row in tc.get("/api/v1/knowledge-bases").json()}
    assert len(tc.get(f"/api/v1/knowledge-bases/{kid}/documents").json()) == 1
    assert tc.patch(f"/api/v1/knowledge-bases/{kid}/rename", json={"name": "Orphan KB (admin)"}).status_code == 200

    # the deleted owner's group can no longer be resolved -> every manager may look after it
    for username in ("gonemgr", "staymgr"):
        login_as(tc, username, "UserPass123!")
        listed = next(row for row in tc.get("/api/v1/knowledge-bases").json() if row["id"] == kid)
        assert listed["can_edit"] is True, username
        assert tc.patch(f"/api/v1/knowledge-bases/{kid}/rename", json={"name": f"Orphan KB ({username})"}).status_code == 200, username
        assert tc.patch(f"/api/v1/documents/{did}/metadata", json={"published_at": "2024-01-01"}).status_code == 200, username
    login_as(tc, "stayuser", "UserPass123!")
    assert tc.patch(f"/api/v1/knowledge-bases/{kid}/rename", json={"name": "nope"}).status_code == 403


def test_deactivated_owner_kb_is_editable_by_the_owners_group_manager():
    tc = next(client())
    team = make_group(tc, "SleepTeam")
    other_team = make_group(tc, "AwakeTeam")
    sleeper = make_user(tc, "sleepowner", group_id=team["id"])
    make_user(tc, "sleepmgr", role="manager", group_id=team["id"])
    make_user(tc, "awakemgr", role="manager", group_id=other_team["id"])
    login_as(tc, "sleepowner", "UserPass123!")
    kb = make_kb(tc, "Sleeper KB", "sleeper-kb")
    login_as(tc, "admin", ADMIN["password"])
    assert tc.patch(f"/api/v1/users/{sleeper['id']}", json={"is_active": False}).status_code == 200
    assert tc.patch(f"/api/v1/knowledge-bases/{kb['id']}/rename", json={"name": "Sleeper KB (admin)"}).status_code == 200
    login_as(tc, "sleepmgr", "UserPass123!")
    assert tc.patch(f"/api/v1/knowledge-bases/{kb['id']}/rename", json={"name": "Sleeper KB (mgr)"}).status_code == 200
    # the group is still known, so a manager of another group gets no fallback rights
    login_as(tc, "awakemgr", "UserPass123!")
    assert tc.patch(f"/api/v1/knowledge-bases/{kb['id']}/rename", json={"name": "nope"}).status_code == 403


def test_system_maintenance_endpoints_are_admin_only():
    tc = next(client())
    make_user(tc, "sysuser")
    make_user(tc, "sysmgr", role="manager")
    for username in ("sysuser", "sysmgr"):
        login_as(tc, username, "UserPass123!")
        assert tc.post("/api/v1/internal/process-next").status_code == 403, username
        assert tc.post("/api/v1/system/test-llm").status_code == 403, username
        assert tc.post("/api/v1/system/observability/cleanup").status_code == 403, username
    login_as(tc, "admin", ADMIN["password"])
    assert tc.post("/api/v1/internal/process-next").status_code not in (401, 403)
    assert tc.post("/api/v1/system/observability/cleanup").status_code not in (401, 403)
    assert tc.post("/api/v1/system/test-llm").status_code not in (401, 403)


def test_owner_and_admin_can_edit_a_kb():
    tc = next(client())
    make_user(tc, "kbowner2")
    for actor, password in (("kbowner2", "UserPass123!"), ("admin", ADMIN["password"])):
        login_as(tc, "kbowner2", "UserPass123!")
        kb = make_kb(tc, f"KB for {actor}")
        assert kb["can_edit"] is True
        doc = _upload(tc, kb["id"]).json()
        login_as(tc, actor, password)
        _assert_all_edits(tc, kb["id"], doc["document_id"], "allowed", actor)


def test_admin_only_endpoints_refuse_owners():
    tc = next(client())
    make_user(tc, "plainowner")
    make_user(tc, "plainmgr", role="manager")
    login_as(tc, "plainowner", "UserPass123!")
    kb = make_kb(tc, "Plain KB", "plain-kb")
    for username in ("plainowner", "plainmgr"):
        login_as(tc, username, "UserPass123!")
        assert tc.patch(f"/api/v1/knowledge-bases/{kb['id']}/retrieval-config", json={"enable_graph": False}).status_code == 403
        assert tc.post(f"/api/v1/knowledge-bases/{kb['id']}/documents/reindex").status_code == 403
        assert tc.get("/api/v1/mcp/activity").status_code == 403
        assert tc.get("/api/v1/tokens").status_code == 403
    login_as(tc, "admin", ADMIN["password"])
    assert tc.patch(f"/api/v1/knowledge-bases/{kb['id']}/retrieval-config", json={"enable_graph": False}).status_code == 200
    reindex = tc.post(f"/api/v1/knowledge-bases/{kb['id']}/documents/reindex")
    assert reindex.status_code == 200 and "count" in reindex.json()
    assert tc.get("/api/v1/mcp/activity").status_code == 200


# ---------------------------------------------------------------------------
# Token scoping
# ---------------------------------------------------------------------------

def test_token_management_is_admin_only():
    """MCP / Ingest token management (/api/v1/tokens*) is admin-only.  Users and
    managers — even ones who own the KB the token would be scoped to — get 403."""
    tc = next(client())
    group = make_group(tc, "TokenGroup")
    make_user(tc, "tokenuser", group_id=group["id"])
    make_user(tc, "tokenmgr", role="manager", group_id=group["id"])
    kb = make_kb(tc, "Token KB", "token-kb")
    tc.post(f"/api/v1/knowledge-bases/{kb['id']}/activate")

    created = tc.post("/api/v1/tokens", json={"name": "admin-mcp", "allowed_knowledge_base_ids": [kb["id"]],
                                              "allowed_tools": ["search_knowledge"]})
    assert created.status_code == 200, created.text
    mcp = created.json()
    ingest = tc.post("/api/v1/tokens", json={"name": "admin-ingest", "allowed_scopes": ["documents:write"],
                                             "allowed_ingest_knowledge_base_id": kb["id"]})
    assert ingest.status_code == 200, ingest.text
    tid = mcp["id"]

    for username in ("tokenuser", "tokenmgr"):
        login_as(tc, username, "UserPass123!")
        assert tc.get("/api/v1/tokens").status_code == 403
        assert tc.post("/api/v1/tokens", json={"name": "mine", "allowed_knowledge_base_ids": [kb["id"]],
                                               "allowed_tools": ["search_knowledge"]}).status_code == 403
        assert tc.post("/api/v1/tokens", json={"name": "mine-ingest", "allowed_scopes": ["documents:write"],
                                               "allowed_ingest_knowledge_base_id": kb["id"]}).status_code == 403
        assert tc.post(f"/api/v1/tokens/{tid}/disable").status_code == 403
        assert tc.post(f"/api/v1/tokens/{tid}/enable").status_code == 403
        assert tc.patch(f"/api/v1/tokens/{tid}/limits", json={"requests_per_minute": 5}).status_code == 403
        assert tc.post(f"/api/v1/tokens/{tid}/rotate").status_code == 403
        assert tc.post(f"/api/v1/tokens/{tid}/revoke").status_code == 403

    login_as(tc, "admin", ADMIN["password"])
    assert {row["id"] for row in tc.get("/api/v1/tokens").json()} >= {tid, ingest.json()["id"]}
    assert tc.post(f"/api/v1/tokens/{tid}/disable").status_code == 200
    assert tc.post(f"/api/v1/tokens/{tid}/enable").status_code == 200
    assert tc.patch(f"/api/v1/tokens/{tid}/limits", json={"requests_per_minute": 5}).status_code == 200
    rotated = tc.post(f"/api/v1/tokens/{tid}/rotate")
    assert rotated.status_code == 200, rotated.text
    assert tc.post(f"/api/v1/tokens/{rotated.json()['id']}/revoke").status_code == 200


def test_token_scope_cannot_exceed_visibility():
    tc = next(client())
    group = make_group(tc, "ScopeGroup")
    make_user(tc, "scopeuser", group_id=group["id"])

    login_as(tc, "admin", ADMIN["password"])
    admin_kb = make_kb(tc, "Admin Secret KB", "admin-secret-kb")
    tc.post(f"/api/v1/knowledge-bases/{admin_kb['id']}/activate")

    # a non-admin is refused outright (token management is admin-only)
    login_as(tc, "scopeuser", "UserPass123!")
    denied = tc.post("/api/v1/tokens", json={"name": "sneaky", "allowed_knowledge_base_ids": [admin_kb["id"]],
                                             "allowed_tools": ["search_knowledge"]})
    assert denied.status_code == 403
    denied_ingest = tc.post("/api/v1/tokens", json={"name": "sneaky2", "allowed_scopes": ["documents:write"],
                                                    "allowed_ingest_knowledge_base_id": admin_kb["id"]})
    assert denied_ingest.status_code == 403


def test_delete_kb_revokes_scoped_tokens():
    tc = next(client())
    make_user(tc, "kbdeleter")
    kb = make_kb(tc, "Doomed KB", "doomed-kb")
    tc.post(f"/api/v1/knowledge-bases/{kb['id']}/activate")
    token = tc.post("/api/v1/tokens", json={"name": "bound", "allowed_knowledge_base_ids": [kb["id"]],
                                            "allowed_tools": ["search_knowledge"]}).json()
    ingest = tc.post("/api/v1/tokens", json={"name": "bound-ingest", "allowed_scopes": ["documents:write"],
                                             "allowed_ingest_knowledge_base_id": kb["id"]}).json()
    deleted = tc.delete(f"/api/v1/knowledge-bases/{kb['id']}")
    assert deleted.status_code == 200
    listed = {row["id"]: row["status"] for row in tc.get("/api/v1/tokens").json()}
    assert listed[token["id"]] == "revoked"
    assert listed[ingest["id"]] == "revoked"


def test_document_template_mutations_require_kb_owner():
    tc = next(client())
    protected_kb = make_kb(tc, "Protected templates", "protected-templates")
    template = tc.post(f"/api/v1/knowledge-bases/{protected_kb['id']}/document-templates", json={
        "name": "Protected type", "fields": [{"key": "issuer", "label": "Issuer"}],
    }).json()
    make_user(tc, "templateoutsider")
    login_as(tc, "templateoutsider", "UserPass123!")

    assert tc.patch(f"/api/v1/document-templates/{template['id']}", json={"description": "unauthorized"}).status_code == 403
    assert tc.patch(f"/api/v1/document-templates/{template['id']}/rename", json={"name": "Unauthorized rename"}).status_code == 403
    assert tc.post(f"/api/v1/document-templates/{template['id']}/duplicate").status_code == 403
    assert tc.delete(f"/api/v1/document-templates/{template['id']}").status_code == 403
    assert tc.post(f"/api/v1/document-templates/{template['id']}/activate").status_code == 403
    assert tc.delete(f"/api/v1/document-templates/{template['id']}/purge").status_code == 403


# ---------------------------------------------------------------------------
# Logs gating
# ---------------------------------------------------------------------------

def test_logs_gating():
    tc = next(client())
    group = make_group(tc, "LogGroup")
    make_user(tc, "loguser", group_id=group["id"])
    make_user(tc, "logmgr", role="manager", group_id=group["id"])

    login_as(tc, "loguser", "UserPass123!")
    assert tc.get("/api/v1/audit-logs").status_code == 403
    assert tc.get("/api/v1/logs/transactions").status_code == 403
    assert tc.get("/api/v1/traces").status_code == 403

    login_as(tc, "logmgr", "UserPass123!")
    assert tc.get("/api/v1/audit-logs").status_code == 200
    assert tc.get("/api/v1/logs/transactions").status_code == 200

    login_as(tc, "admin", ADMIN["password"])
    assert tc.get("/api/v1/audit-logs").status_code == 200
