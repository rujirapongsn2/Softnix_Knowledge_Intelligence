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
os.environ["OPENROUTER_API_KEY"] = ""
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
# KB visibility matrix
# ---------------------------------------------------------------------------

def test_kb_visibility_matrix():
    tc = next(client())
    group = make_group(tc, "Legal")
    make_user(tc, "nee", group_id=group["id"])          # role user
    make_user(tc, "somchai", role="manager", group_id=group["id"])
    make_user(tc, "pong")                                # role user, no group

    # admin creates a KB -> admin-owned
    admin_kb = make_kb(tc, "Admin KB", "admin-kb")

    # nee (user, Legal) creates own KB
    login_as(tc, "nee", "UserPass123!")
    nee_kb = make_kb(tc, "Nee KB", "nee-kb")
    visible = {kb["code"] for kb in tc.get("/api/v1/knowledge-bases").json()}
    assert visible == {"nee-kb"}, visible

    # nee cannot touch admin's KB (404 anti-enumeration)
    assert tc.patch(f"/api/v1/knowledge-bases/{admin_kb['id']}/icon", json={"icon": "auto"}).status_code == 404
    assert tc.post(f"/api/v1/knowledge-bases/{admin_kb['id']}/activate").status_code == 404
    # and cannot see its documents
    assert tc.get(f"/api/v1/knowledge-bases/{admin_kb['id']}/documents").status_code == 404

    # pong (user, no group) sees nothing
    login_as(tc, "pong", "UserPass123!")
    assert tc.get("/api/v1/knowledge-bases").json() == []

    # somchai (manager, Legal) sees every KB with an owner in Legal (nee's KB)
    login_as(tc, "somchai", "UserPass123!")
    manager_visible = {kb["code"] for kb in tc.get("/api/v1/knowledge-bases").json()}
    assert manager_visible == {"nee-kb"}, manager_visible
    assert tc.post(f"/api/v1/knowledge-bases/{nee_kb['id']}/activate").status_code == 200

    # admin sees everything
    login_as(tc, "admin", ADMIN["password"])
    admin_visible = {kb["code"] for kb in tc.get("/api/v1/knowledge-bases").json()}
    assert admin_visible == {"admin-kb", "nee-kb"}, admin_visible


def test_kb_ownership_written_on_create():
    tc = next(client())
    make_user(tc, "owner1")
    login_as(tc, "owner1", "UserPass123!")
    kb = make_kb(tc, "Owned KB", "owned-kb")
    from app.db import SessionLocal
    with SessionLocal() as db:
        row = db.query(KbOwner).filter_by(kb_id=kb["id"]).one()
        assert row.user_id == db.query(User).filter_by(username="owner1").one().id


# ---------------------------------------------------------------------------
# Query scoping
# ---------------------------------------------------------------------------

def test_query_respects_kb_visibility():
    tc = next(client())
    group = make_group(tc, "QGroup")
    make_user(tc, "quser", group_id=group["id"])
    make_user(tc, "outsider")

    login_as(tc, "quser", "UserPass123!")
    kb = make_kb(tc, "Q KB", "q-kb")
    tc.post(f"/api/v1/knowledge-bases/{kb['id']}/activate")

    login_as(tc, "outsider", "UserPass123!")
    response = tc.post("/api/v1/query", json={"query": "anything", "knowledge_base_ids": [kb["id"]]})
    assert response.status_code == 404
    # empty list stays within the caller's visible KBs — outsider has none,
    # so the request is rejected instead of silently searching other people's KBs
    response = tc.post("/api/v1/query", json={"query": "anything", "knowledge_base_ids": []})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "KNOWLEDGE_BASE_NOT_VISIBLE"


# ---------------------------------------------------------------------------
# Token scoping
# ---------------------------------------------------------------------------

def test_token_creator_scoping():
    tc = next(client())
    group = make_group(tc, "TokenGroup")
    make_user(tc, "tokenuser", group_id=group["id"])
    make_user(tc, "tokenmgr", role="manager", group_id=group["id"])

    login_as(tc, "tokenuser", "UserPass123!")
    kb = make_kb(tc, "Token KB", "token-kb")
    tc.post(f"/api/v1/knowledge-bases/{kb['id']}/activate")

    # own-KB token is fine
    created = tc.post("/api/v1/tokens", json={"name": "mine", "allowed_knowledge_base_ids": [kb["id"]],
                                              "allowed_tools": ["search_knowledge"]})
    assert created.status_code == 200, created.text
    mine = created.json()

    # listing shows only tokens you created
    listed = tc.get("/api/v1/tokens").json()
    assert {row["id"] for row in listed} == {mine["id"]}

    # manager (same group) sees it and can disable it
    login_as(tc, "tokenmgr", "UserPass123!")
    listed = tc.get("/api/v1/tokens").json()
    assert {row["id"] for row in listed} == {mine["id"]}
    assert tc.post(f"/api/v1/tokens/{mine['id']}/disable").status_code == 200

    # an unrelated user sees nothing and cannot touch it
    login_as(tc, "admin", ADMIN["password"])
    make_user(tc, "outsider2")
    login_as(tc, "outsider2", "UserPass123!")
    assert tc.get("/api/v1/tokens").json() == []
    assert tc.post(f"/api/v1/tokens/{mine['id']}/enable").status_code == 404


def test_token_scope_cannot_exceed_visibility():
    tc = next(client())
    group = make_group(tc, "ScopeGroup")
    make_user(tc, "scopeuser", group_id=group["id"])

    login_as(tc, "admin", ADMIN["password"])
    admin_kb = make_kb(tc, "Admin Secret KB", "admin-secret-kb")
    tc.post(f"/api/v1/knowledge-bases/{admin_kb['id']}/activate")

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
    login_as(tc, "kbdeleter", "UserPass123!")
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
