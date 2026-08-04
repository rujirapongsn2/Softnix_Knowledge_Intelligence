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

from fastapi.testclient import TestClient
from app.main import app

INGEST_SCOPE = "documents:write"


def client():
    with TestClient(app) as test_client:
        assert test_client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        yield test_client


def _knowledge_base(test_client, code: str) -> str:
    """Create an active Knowledge Base under a code unique to this file.

    Every test module shares one sqlite database, so codes are namespaced and all
    assertions look up documents through their Knowledge Base.
    """
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": code, "code": code}).json()
    assert test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/activate").status_code == 200
    return kb["id"]


_UNSET = object()


def _token(test_client, kb_ids, scopes=(INGEST_SCOPE,), tools=(), allowed_ingest_kb_id=_UNSET, **overrides) -> str:
    kb_ids = list(kb_ids)
    scopes = list(scopes)
    # allowed_ingest_knowledge_base_id is a dedicated write-scope axis, separate
    # from allowed_knowledge_base_ids (the MCP read axis); default it to the
    # first requested KB only when a caller is actually asking for write access.
    if allowed_ingest_kb_id is _UNSET:
        allowed_ingest_kb_id = kb_ids[0] if (INGEST_SCOPE in scopes and kb_ids) else None
    payload = {"name": "ingest-agent", "allowed_knowledge_base_ids": kb_ids,
               "allowed_tools": list(tools), "allowed_scopes": scopes,
               "allowed_ingest_knowledge_base_id": allowed_ingest_kb_id, **overrides}
    response = test_client.post("/api/v1/tokens", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _headers(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def _drain(test_client) -> None:
    while test_client.post("/api/v1/internal/process-next").json()["processed"]:
        pass


def _upload(test_client, secret: str, kb_id: str, name: str = "note.txt", body: bytes = b"Customer Portal runs on APP-01."):
    return test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents",
                            headers=_headers(secret), files={"file": (name, body, "text/plain")})


def test_token_without_ingest_scope_cannot_write():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-scope")
    # A token with no tools used to mean "every tool" before the wildcard was
    # removed; create_token() now rejects that combination outright (it would
    # otherwise mint a credential with zero capability), so only a token that
    # is granted an MCP tool can even be issued here, and it still may not
    # reach the ingest surface without documents:write.
    response = test_client.post("/api/v1/tokens", json={
        "name": "no-capability", "allowed_knowledge_base_ids": [kb_id], "allowed_tools": [], "allowed_scopes": []})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "TOKEN_NO_CAPABILITY"
    secret = _token(test_client, [kb_id], scopes=(), tools=["search_knowledge"])
    response = _upload(test_client, secret, kb_id)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_SCOPE_NOT_ALLOWED"
    documents = test_client.get(f"/api/v1/knowledge-bases/{kb_id}/documents").json()
    assert documents == []


def test_ingest_token_requires_explicit_knowledge_base_scope():
    test_client = next(client())
    _knowledge_base(test_client, "ingest-api-unscoped")
    # documents:write with no ingest Knowledge Base is now rejected at issue
    # time, not at upload time, since the field is required whenever the scope
    # is requested.
    response = test_client.post("/api/v1/tokens", json={
        "name": "unscoped", "allowed_knowledge_base_ids": [], "allowed_scopes": [INGEST_SCOPE]})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INGEST_KNOWLEDGE_BASE_REQUIRED"


def test_ingest_write_scope_is_independent_of_mcp_read_kb_list():
    test_client = next(client())
    mcp_kb = _knowledge_base(test_client, "ingest-api-mcp-axis")
    write_kb = _knowledge_base(test_client, "ingest-api-write-axis")
    # A write-only token (no allowed_tools) has its allowed_knowledge_base_ids
    # (the MCP read axis) force-cleared at issue time, since authorize() would
    # never reach that list without a granted tool. Passing extra ids here must
    # not leak into ingest scope, which stays pinned to exactly one Knowledge Base.
    secret = _token(test_client, [mcp_kb, write_kb], allowed_ingest_kb_id=write_kb)
    assert _upload(test_client, secret, write_kb).status_code == 202
    denied = _upload(test_client, secret, mcp_kb)
    assert denied.status_code == 404
    called = test_client.post("/mcp", headers=_headers(secret), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_knowledge", "arguments": {"query": "anything"}}})
    assert called.json()["error"]["code"] == "AUTH_TOOL_NOT_ALLOWED"


def test_token_cannot_have_both_mcp_tools_and_ingest_scope():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-capability-conflict")
    kb_ids = [kb_id]
    payload = {"name": "mixed", "allowed_knowledge_base_ids": kb_ids, "allowed_tools": ["search_knowledge"],
               "allowed_scopes": [INGEST_SCOPE], "allowed_ingest_knowledge_base_id": kb_id}
    response = test_client.post("/api/v1/tokens", json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "TOKEN_CAPABILITY_CONFLICT"


def test_ingest_knowledge_base_must_be_active_and_cannot_be_set_without_scope():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-inactive-target")
    assert test_client.post(f"/api/v1/knowledge-bases/{kb_id}/disable").status_code == 200
    inactive = test_client.post("/api/v1/tokens", json={
        "name": "inactive-target", "allowed_knowledge_base_ids": [], "allowed_scopes": [INGEST_SCOPE],
        "allowed_ingest_knowledge_base_id": kb_id})
    assert inactive.status_code == 400
    assert inactive.json()["error"]["code"] == "KNOWLEDGE_BASE_INACTIVE"

    other_kb = _knowledge_base(test_client, "ingest-api-no-scope-target")
    no_scope = test_client.post("/api/v1/tokens", json={
        "name": "no-scope", "allowed_knowledge_base_ids": [], "allowed_scopes": [],
        "allowed_ingest_knowledge_base_id": other_kb})
    assert no_scope.status_code == 400
    assert no_scope.json()["error"]["code"] == "INGEST_KNOWLEDGE_BASE_NOT_ALLOWED"


def test_ingest_uploads_and_reports_completion():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-happy")
    secret = _token(test_client, [kb_id])
    response = _upload(test_client, secret, kb_id, "architecture.txt")
    assert response.status_code == 202
    queued = response.json()
    assert queued["status"] == "queued" and queued["document_type"] == "general"

    pending = test_client.get(f"/api/v1/ingest/documents/{queued['document_id']}", headers=_headers(secret)).json()
    assert pending["status"] == "queued" and pending["latest_job"]["status"] == "queued"
    _drain(test_client)

    done = test_client.get(f"/api/v1/ingest/documents/{queued['document_id']}", headers=_headers(secret)).json()
    assert done["status"] == "completed" and done["knowledge_base_id"] == kb_id
    assert done["latest_job"]["progress_percent"] == 100
    jobs = test_client.get(f"/api/v1/ingest/documents/{queued['document_id']}/jobs", headers=_headers(secret)).json()
    assert any(job["id"] == queued["job_id"] and job["status"] == "completed" for job in jobs)
    listed = test_client.get(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents?status=completed", headers=_headers(secret)).json()
    assert listed["total"] == 1 and listed["items"][0]["document_id"] == queued["document_id"]


def test_ingest_token_cannot_reach_another_knowledge_base():
    test_client = next(client())
    granted = _knowledge_base(test_client, "ingest-api-granted")
    other = _knowledge_base(test_client, "ingest-api-other")
    secret = _token(test_client, [granted])
    assert _upload(test_client, secret, other).status_code == 404

    other_secret = _token(test_client, [other])
    foreign = _upload(test_client, other_secret, other, "foreign.txt", b"Foreign knowledge.").json()
    leaked = test_client.get(f"/api/v1/ingest/documents/{foreign['document_id']}", headers=_headers(secret))
    assert leaked.status_code == 404
    assert leaked.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"
    assert test_client.get(f"/api/v1/ingest/documents/{foreign['document_id']}/jobs", headers=_headers(secret)).status_code == 404


def test_ingest_batch_isolates_per_file_failures():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-batch")
    secret = _token(test_client, [kb_id])
    response = test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents/batch", headers=_headers(secret), files=[
        ("files", ("one.txt", b"First document.", "text/plain")),
        ("files", ("two.md", b"# Second document", "text/markdown")),
        ("files", ("three.exe", b"binary", "application/octet-stream")),
    ])
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "partial" and body["queued_count"] == 2 and body["failed_count"] == 1
    rejected = next(item for item in body["results"] if item["filename"] == "three.exe")
    assert rejected["error_code"] == "FILE_TYPE_NOT_SUPPORTED"

    too_many = test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents/batch", headers=_headers(secret),
                                files=[("files", (f"file-{index}.txt", b"body", "text/plain")) for index in range(21)])
    assert too_many.status_code == 400
    assert too_many.json()["error"]["code"] == "BATCH_TOO_MANY_FILES"


def test_ingest_rejects_duplicate_and_unsupported_files():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-rejects")
    secret = _token(test_client, [kb_id])
    assert _upload(test_client, secret, kb_id, "same.txt", b"Identical body.").status_code == 202
    duplicate = _upload(test_client, secret, kb_id, "same.txt", b"Identical body.")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "FILE_DUPLICATE"
    unsupported = _upload(test_client, secret, kb_id, "payload.exe", b"binary")
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["code"] == "FILE_TYPE_NOT_SUPPORTED"
    rejections = test_client.get("/api/v1/audit-logs?limit=100").json()
    assert any(row["action"] == "document.ingest.rejected" and row["metadata"].get("error_code") == "FILE_DUPLICATE" for row in rejections)


def test_ingest_rejects_disabled_knowledge_base():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-disabled")
    secret = _token(test_client, [kb_id])
    assert test_client.post(f"/api/v1/knowledge-bases/{kb_id}/disable").status_code == 200
    response = _upload(test_client, secret, kb_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "KNOWLEDGE_BASE_DISABLED"


def test_ingest_lists_only_its_own_knowledge_base():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-kb-list")
    other_kb_id = _knowledge_base(test_client, "ingest-api-kb-list-other")
    secret = _token(test_client, [kb_id])
    listed = test_client.get("/api/v1/ingest/knowledge-bases", headers=_headers(secret)).json()
    assert listed == {"items": [{"id": kb_id, "code": "ingest-api-kb-list", "name": "ingest-api-kb-list", "status": "active"}]}
    assert other_kb_id not in [item["id"] for item in listed["items"]]

    # A disabled KB is still reported (with its status) rather than hidden, so a
    # client can tell "nothing configured" apart from "configured but paused".
    assert test_client.post(f"/api/v1/knowledge-bases/{kb_id}/disable").status_code == 200
    disabled_listing = test_client.get("/api/v1/ingest/knowledge-bases", headers=_headers(secret)).json()
    assert disabled_listing["items"][0]["status"] == "disabled"


def test_ingest_rejects_invalid_credentials():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-credentials")
    missing = test_client.post(f"/api/v1/ingest/knowledge-bases/{kb_id}/documents", files={"file": ("a.txt", b"body", "text/plain")})
    assert missing.status_code == 401 and missing.json()["error"]["code"] == "AUTH_TOKEN_MISSING"
    assert _upload(test_client, "skik_live_not-a-real-token", kb_id).json()["error"]["code"] == "AUTH_TOKEN_INVALID"

    revoked = test_client.post("/api/v1/tokens", json={
        "name": "revoked", "allowed_knowledge_base_ids": [kb_id], "allowed_scopes": [INGEST_SCOPE],
        "allowed_ingest_knowledge_base_id": kb_id}).json()
    assert test_client.post(f"/api/v1/tokens/{revoked['id']}/revoke").status_code == 200
    assert _upload(test_client, revoked["token"], kb_id).json()["error"]["code"] == "AUTH_TOKEN_REVOKED"

    disabled = test_client.post("/api/v1/tokens", json={
        "name": "disabled", "allowed_knowledge_base_ids": [kb_id], "allowed_scopes": [INGEST_SCOPE],
        "allowed_ingest_knowledge_base_id": kb_id}).json()
    assert test_client.post(f"/api/v1/tokens/{disabled['id']}/disable").status_code == 200
    assert _upload(test_client, disabled["token"], kb_id).json()["error"]["code"] == "AUTH_TOKEN_INVALID"


def test_write_only_token_cannot_call_mcp_tools():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-writeonly")
    secret = _token(test_client, [kb_id])
    listed = test_client.post("/mcp", headers=_headers(secret), json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    assert listed["result"]["tools"] == []
    called = test_client.post("/mcp", headers=_headers(secret), json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_knowledge", "arguments": {"query": "anything"}}})
    assert called.json()["error"]["code"] == "AUTH_TOOL_NOT_ALLOWED"


def test_rotation_preserves_ingest_scope_and_retires_old_secret():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-rotate")
    created = test_client.post("/api/v1/tokens", json={
        "name": "rotating", "allowed_knowledge_base_ids": [kb_id], "allowed_scopes": [INGEST_SCOPE],
        "allowed_ingest_knowledge_base_id": kb_id}).json()
    rotated = test_client.post(f"/api/v1/tokens/{created['id']}/rotate").json()
    assert rotated["allowed_scopes"] == [INGEST_SCOPE]
    assert rotated["allowed_ingest_knowledge_base_id"] == kb_id
    assert _upload(test_client, rotated["token"], kb_id, "rotated.txt", b"After rotation.").status_code == 202
    assert _upload(test_client, created["token"], kb_id).json()["error"]["code"] == "AUTH_TOKEN_REVOKED"


def test_unknown_tool_or_scope_is_rejected_at_issue_time():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-validation")
    bad_tool = test_client.post("/api/v1/tokens", json={"name": "typo", "allowed_knowledge_base_ids": [kb_id], "allowed_tools": ["serch_knowledge"]})
    assert bad_tool.status_code == 400 and bad_tool.json()["error"]["code"] == "TOKEN_TOOL_UNKNOWN"
    bad_scope = test_client.post("/api/v1/tokens", json={"name": "typo", "allowed_knowledge_base_ids": [kb_id], "allowed_scopes": ["documents:admin"]})
    assert bad_scope.status_code == 400 and bad_scope.json()["error"]["code"] == "TOKEN_SCOPE_UNKNOWN"


def test_ingest_rate_limit_is_charged_per_token():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-ratelimit")
    secret = _token(test_client, [kb_id], requests_per_minute=1)
    assert _upload(test_client, secret, kb_id, "first.txt", b"First body.").status_code == 202
    limited = _upload(test_client, secret, kb_id, "second.txt", b"Second body.")
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "MCP_RATE_LIMITED"
    assert limited.json()["error"]["retryable"] is True


def test_ingest_observability_records_attribution_without_the_secret():
    test_client = next(client())
    kb_id = _knowledge_base(test_client, "ingest-api-observability")
    secret = _token(test_client, [kb_id])
    uploaded = _upload(test_client, secret, kb_id, "audited.txt", b"Audited body.").json()

    audit = test_client.get("/api/v1/audit-logs?limit=100").json()
    entry = next(row for row in audit if row["action"] == "document.upload" and row["target_id"] == uploaded["document_id"])
    assert entry["metadata"]["transport"] == "ingest_api" and entry["metadata"]["token_name"] == "ingest-agent"
    assert secret not in str(audit)

    transactions = test_client.get("/api/v1/logs/transactions?limit=100").json()
    ingest_transaction = next(row for row in transactions if row["path"].startswith("/api/v1/ingest"))
    assert ingest_transaction["authentication"] == "ingest_token"
    assert secret not in str(transactions)
