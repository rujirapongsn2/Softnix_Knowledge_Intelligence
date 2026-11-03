import base64
from datetime import datetime
import json
import uuid

from fastapi.testclient import TestClient
import pytest

from app.db import Base, engine, SessionLocal
from app.main import app
from app.models import AuditLog, Document, Group, KbOwner, KnowledgeBase, TokenKey, User
from app.security import require_manager


@pytest.fixture
def history():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]
        group = Group(name=f"Audit team {suffix}")
        db.add(group); db.flush()
        admin = User(username=f"audit-admin-{suffix}", password_hash="unused", role="admin")
        manager = User(username=f"audit-manager-{suffix}", password_hash="unused", role="manager", group_id=group.id)
        outsider = User(username=f"audit-other-{suffix}", password_hash="unused", role="manager")
        db.add_all([admin, manager, outsider]); db.flush()
        own = KnowledgeBase(code=f"audit-own-{suffix}", name=f"Audit own {suffix}")
        other = KnowledgeBase(code=f"audit-other-{suffix}", name=f"Audit private {suffix}")
        db.add_all([own, other]); db.flush()
        db.add(KbOwner(kb_id=own.id, user_id=manager.id))
        docs = []
        for kb, title in [(own, "Policy Document"), (other, "Private Document")]:
            doc = Document(knowledge_base_id=kb.id, original_filename=f"{title}.md", stored_filename="source.md",
                           storage_path="/no-file", mime_type="text/markdown", file_size=20,
                           checksum_sha256=uuid.uuid4().hex * 2, title=title, status="completed")
            db.add(doc); docs.append(doc)
        db.flush()
        token = TokenKey(name=f"Audit sender {suffix}", token_prefix="test", token_hash=uuid.uuid4().hex)
        db.add(token); db.flush()
        stamp = datetime(2026, 10, 25, 3)
        logs = []
        for action in ["document.upload", "document.metadata.update", "document.delete", "document.restore"]:
            log = AuditLog(actor_user_id=manager.id, action=action, target_type="document", target_id=docs[0].id,
                           created_at=stamp, metadata_json={"metadata_fields": ["issuer"], "secret": "never-show", "metadata": {"secret": "never-show"}})
            db.add(log); logs.append(log)
        db.add(AuditLog(action="document.upload", actor_user_id=manager.id, target_type="document",
                        target_id=docs[1].id, created_at=stamp, metadata_json={}))
        for name in ("Before rename", "After rename"):
            db.add(AuditLog(action="document.reference_file.attach", target_type="document", target_id=docs[0].id,
                            created_at=stamp, metadata_json={"transport": "ingest_api", "token_id": token.id, "token_name": name}))
        db.add(AuditLog(action="document.ingest.rejected", target_type="token", target_id=token.id,
                        created_at=stamp, metadata_json={"knowledge_base_id": own.id, "token_name": token.name,
                                                      "filename": "rejected.txt", "error_code": "FILE_TOO_LARGE"}))
        db.add(AuditLog(action="request.transaction", target_type="http_request", created_at=stamp, metadata_json={}))
        db.commit()
        data = {"own": own.id, "other": other.id, "doc": docs[0].id, "private_doc": docs[1].id,
                "manager": manager, "admin": admin, "outsider": outsider, "token": token.id,
                "token_name": token.name, "logs": [log.id for log in logs]}
        for user in (manager, admin, outsider): db.expunge(user)
    def as_user(user):
        app.dependency_overrides[require_manager] = lambda: user
        return TestClient(app)
    yield data, as_user
    app.dependency_overrides.pop(require_manager, None)


def test_manager_scope_filters_count_options_and_private_search(history):
    data, as_user = history
    client = as_user(data["manager"])
    body = client.get("/api/v1/logs/audit", params={"knowledge_base_id": data["own"]}).json()
    assert body["total"] == len(body["items"]) == 7
    assert {row["id"] for row in body["filter_options"]["knowledge_bases"]} == {data["own"]}
    assert "never-show" not in json.dumps(body)
    assert all(item["action"].startswith("document.") for item in body["items"])
    assert client.get("/api/v1/logs/audit", params={"knowledge_base_id": data["other"]}).json()["total"] == 0
    assert client.get("/api/v1/logs/audit", params={"search": "Private Document"}).json()["total"] == 0
    assert len([actor for actor in body["filter_options"]["actors"] if actor["id"] == data["token"]]) == 1
    assert next(actor for actor in body["filter_options"]["actors"] if actor["id"] == data["token"])["name"] == data["token_name"]
    assert as_user(data["outsider"]).get("/api/v1/logs/audit").json()["total"] == 0
    assert as_user(data["admin"]).get("/api/v1/logs/audit", params={"knowledge_base_id": data["other"]}).json()["total"] == 1


def test_combined_filters_channels_and_timezone(history):
    data, as_user = history
    client = as_user(data["manager"])
    response = client.get("/api/v1/logs/audit", params={"knowledge_base_id": data["own"], "document_id": data["doc"],
                          "action": "document.metadata.update", "actor_type": "user", "actor_id": data["manager"].id,
                          "channel": "gui", "search": "Policy", "from_ts": "2026-10-25T09:59:00+07:00", "to_ts": "2026-10-25T10:01:00+07:00"})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["created_at"].endswith("Z")
    token_events = client.get("/api/v1/logs/audit", params={"actor_type": "token", "actor_id": data["token"], "channel": "ingest_api"}).json()
    assert token_events["total"] == 3
    assert next(row for row in token_events["items"] if row["action"] == "document.ingest.rejected")["details"]["filename"] == "rejected.txt"


def test_equal_timestamp_cursor_no_skips_or_duplicates(history):
    data, as_user = history
    client = as_user(data["manager"])
    ids, cursor = [], None
    while True:
        params = {"knowledge_base_id": data["own"], "limit": 2}
        if cursor: params["cursor"] = cursor
        response = client.get("/api/v1/logs/audit", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        ids.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        if not cursor: break
    assert len(ids) == len(set(ids)) == 7
    assert set(data["logs"]).issubset(ids)


def test_deleted_document_and_snapshot_fallback_remain_visible(history):
    data, as_user = history
    with SessionLocal() as db:
        doc = db.get(Document, data["doc"])
        doc.deleted_at = datetime.utcnow()
        db.add(AuditLog(action="document.upload", target_type="document", target_id="gone-document",
                        metadata_json={"knowledge_base_id": data["own"], "filename": "gone.md", "document_title": "Old snapshot"}))
        db.commit()
    client = as_user(data["manager"])
    deleted = client.get("/api/v1/logs/audit", params={"document_id": data["doc"]}).json()["items"]
    assert deleted and all(item["document"]["deleted"] and not item["document"]["available"] for item in deleted)
    gone = client.get("/api/v1/logs/audit", params={"document_id": "gone-document"}).json()["items"][0]
    assert gone["document"]["filename"] == "gone.md" and gone["document"]["title"] == "Old snapshot"
    assert not gone["document"]["available"]


@pytest.mark.parametrize("params", [
    {"from_ts": "bad"}, {"from_ts": "2026-10-25"},
    {"from_ts": "0001-01-01T00:00:00+01:00"}, {"to_ts": "9999-12-31T23:59:59-01:00"},
    {"from_ts": "2026-10-26T00:00:00Z", "to_ts": "2026-10-25T00:00:00Z"},
    {"limit": 0}, {"limit": 101}, {"channel": "secret"}, {"actor_type": "unknown"}, {"cursor": "bad"},
    *[{"cursor": base64.urlsafe_b64encode(json.dumps(value).encode()).decode()} for value in
      [["", "id"], "ab", {"2026-10-25T00:00:00Z": 1, "id": 2}, [None, "id"], ["2026-10-25T00:00:00Z"],
       ["0001-01-01T00:00:00+01:00", "id"]]],
])
def test_invalid_audit_filters_return_400(history, params):
    data, as_user = history
    response = as_user(data["admin"]).get("/api/v1/logs/audit", params=params)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "AUDIT_FILTER_INVALID"


def test_audit_read_requires_authenticated_manager():
    with TestClient(app) as client:
        assert client.get("/api/v1/logs/audit").status_code == 401


def test_gui_document_write_endpoints_appear_in_audit():
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        suffix = uuid.uuid4().hex[:8]
        kb = client.post("/api/v1/knowledge-bases", json={"name": "Audit write workflow", "code": f"audit-workflow-{suffix}"}).json()
        client.post(f"/api/v1/knowledge-bases/{kb['id']}/activate")
        uploaded = client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("workflow.txt", f"Audit source {suffix}".encode(), "text/plain")})
        assert uploaded.status_code == 200, uploaded.text
        document_id = uploaded.json()["document_id"]
        changed = client.patch(f"/api/v1/documents/{document_id}/metadata", json={"published_at": "2026-10-25"})
        assert changed.status_code == 200, changed.text
        assert client.delete(f"/api/v1/documents/{document_id}").status_code == 200
        deleted = client.get("/api/v1/logs/audit", params={"document_id": document_id}).json()
        assert all(item["document"]["deleted"] for item in deleted["items"])
        assert client.post(f"/api/v1/documents/{document_id}/restore").status_code == 200
        body = client.get("/api/v1/logs/audit", params={"document_id": document_id}).json()
        assert {item["action"] for item in body["items"]} == {"document.upload", "document.metadata.update", "document.delete", "document.restore"}
        assert all(item["knowledge_base"]["id"] == kb["id"] and item["actor"]["type"] == "user" for item in body["items"])
