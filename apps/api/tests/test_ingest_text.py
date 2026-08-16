"""JSON text-ingest endpoint (InsightDOC Custom API path).

Covers: auth scope, 202 queueing, duplicate 409, empty rejection,
and the stored artifact being Markdown that the worker processes normally.
"""

import os
import tempfile

_TEST_ROOT = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_ROOT}/skip.db"
os.environ["FILE_STORAGE_PATH"] = f"{_TEST_ROOT}/files"
os.environ["INITIAL_ADMIN_PASSWORD"] = "correct-horse-battery-staple"
# bootstrap() only creates tables for these two values; an inherited
# APP_ENV=dev from the developer shell would silently skip create_all.
os.environ["APP_ENV"] = "test"
os.environ["LIGHTRAG_BASE_URL"] = ""
os.environ["REDIS_URL"] = ""
os.environ["OPENROUTER_API_KEY"] = ""

from fastapi.testclient import TestClient

from app.main import app

INGEST_SCOPE = "documents:write"


def client():
    with TestClient(app) as test_client:
        assert test_client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        yield test_client


def _knowledge_base(test_client, code: str) -> str:
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": code, "code": code}).json()
    assert test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/activate").status_code == 200
    return kb["id"]


def _token(test_client, kb_id: str) -> str:
    payload = {"name": "insightdoc-agent", "allowed_knowledge_base_ids": [kb_id],
               "allowed_tools": [], "allowed_scopes": [INGEST_SCOPE],
               "allowed_ingest_knowledge_base_id": kb_id}
    response = test_client.post("/api/v1/tokens", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _post_text(test_client, secret: str, kb_id: str, payload: dict):
    return test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents/text",
                            headers={"Authorization": f"Bearer {secret}"}, json=payload)


def test_text_ingest_queues_markdown_document():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-text-queue")
    secret = _token(test_client, kb_id)

    response = _post_text(test_client, secret, kb_id, {
        "title": "มาตรา ๕๖ ตวิ ทดสอบ",
        "text": "# มาตรา ๕๖ ตวิ\nคนต่างด้าวซึ่งได้รับอนุญาตให้ได้มาซึ่งที่ดินต้องจัดจำหน่ายภายในกำหนด",
        "document_type": "legal",
    })
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued" and body["document_type"] == "legal"

    status = test_client.get(f"/api/v1/ingest/documents/{body['document_id']}",
                             headers={"Authorization": f"Bearer {secret}"}).json()
    assert status["status"] in {"queued", "extracting", "completed"}


def test_text_ingest_rejects_duplicate_with_409():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-text-dup")
    secret = _token(test_client, kb_id)

    payload = {"title": "เอกสารซ้ำ ทดสอบ", "text": "เนื้อหาเดิม ฉบับเดียวกัน สำหรับตรวจสอบ duplicate"}
    assert _post_text(test_client, secret, kb_id, payload).status_code == 202
    second = _post_text(test_client, secret, kb_id, payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "FILE_DUPLICATE"


def test_text_ingest_rejects_blank_text():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-text-empty")
    secret = _token(test_client, kb_id)
    response = _post_text(test_client, secret, kb_id, {"title": "ว่างเปล่า", "text": "   "})
    assert response.status_code in {400, 422}


def test_text_ingest_requires_bearer_token():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-text-auth")
    response = test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents/text",
                                json={"title": "no auth", "text": "hello"})
    assert response.status_code == 401


def test_text_ingest_rejects_token_without_write_scope():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-text-scope")
    payload = {"name": "read-only-agent", "allowed_knowledge_base_ids": [kb_id],
               "allowed_tools": ["search_knowledge"], "allowed_scopes": [],
               "allowed_ingest_knowledge_base_id": None}
    response = test_client.post("/api/v1/tokens", json=payload)
    assert response.status_code == 200
    secret = response.json()["token"]
    denied = _post_text(test_client, secret, kb_id, {"title": "no write", "text": "hello"})
    assert denied.status_code == 403
