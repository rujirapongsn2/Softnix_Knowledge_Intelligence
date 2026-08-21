"""System dashboard tests: role gating + RBAC scoping of every aggregate.

Follows the same self-contained bootstrap pattern as test_rbac.py: a scratch
SQLite database per module, the initial admin from env, TestClient with a
logged-in session.  Documents/jobs/audit rows are seeded directly through
SQLAlchemy so the assertions exercise the endpoint's queries, not the upload
pipeline.
"""
import os
import tempfile
import uuid

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
from app.models import AuditLog, Document, ProcessingJob, User

ADMIN = {"username": "admin", "password": "correct-horse-battery-staple"}


def client():
    with TestClient(app) as test_client:
        assert test_client.post("/api/v1/auth/login", json=ADMIN).status_code == 200
        yield test_client


import pytest


@pytest.fixture(autouse=True)
def _fresh_db():
    """Each test gets a clean database (tests share one process-wide SQLite)."""
    from app.db import Base, engine, SessionLocal
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        settings = __import__('app.config', fromlist=['get_settings']).get_settings()
        db.add(User(username=settings.initial_admin_username,
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


def login_as(tc: TestClient, username: str, password: str = "UserPass123!") -> TestClient:
    response = tc.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return tc


def seed_document(db, kb_id: str, *, status: str = "completed", error_code: str | None = None,
                  deleted: bool = False, title: str = "doc") -> str:
    doc_id = str(uuid.uuid4())
    db.add(Document(
        id=doc_id, knowledge_base_id=kb_id,
        original_filename=f"{title}.pdf", stored_filename=f"{doc_id}.pdf",
        storage_path=f"{doc_id}.pdf", mime_type="application/pdf", file_size=1024,
        checksum_sha256=uuid.uuid4().hex, title=title, status=status, error_code=error_code,
        deleted_at=__import__("datetime").datetime.utcnow() if deleted else None,
    ))
    return doc_id


def seed_job(db, kb_id: str, document_id: str | None, *, status: str = "queued",
             error_code: str | None = None) -> str:
    job_id = str(uuid.uuid4())
    db.add(ProcessingJob(id=job_id, document_id=document_id, knowledge_base_id=kb_id,
                         status=status, error_code=error_code))
    return job_id


def seed_rejection(db, kb_id: str, filename: str, error_code: str = "FILE_DUPLICATE") -> str:
    audit_id = str(uuid.uuid4())
    db.add(AuditLog(id=audit_id, action="document.ingest.rejected",
                    metadata_json={"knowledge_base_id": kb_id, "filename": filename,
                                   "error_code": error_code, "token_name": "tok"}))
    return audit_id


# ---------------------------------------------------------------------------
# Role gating
# ---------------------------------------------------------------------------

def test_dashboard_role_gating():
    tc = next(client())
    group = make_group(tc, "DashGroup")
    make_user(tc, "dashuser", group_id=group["id"])
    make_user(tc, "dashmgr", role="manager", group_id=group["id"])

    # plain users are rejected
    login_as(tc, "dashuser")
    assert tc.get("/api/v1/system/dashboard").status_code == 403

    # manager and admin pass
    login_as(tc, "dashmgr")
    assert tc.get("/api/v1/system/dashboard").status_code == 200
    login_as(tc, "admin", ADMIN["password"])
    assert tc.get("/api/v1/system/dashboard").status_code == 200


# ---------------------------------------------------------------------------
# Scoping: a manager sees only the group's KBs, never another tenant's data
# ---------------------------------------------------------------------------

def test_dashboard_manager_scope_excludes_other_tenants():
    tc = next(client())
    group = make_group(tc, "DashScope")
    make_user(tc, "scopemgr", role="manager", group_id=group["id"])
    make_user(tc, "owner", group_id=group["id"])

    # owner (in scope group) creates one KB; admin creates another
    login_as(tc, "owner")
    scoped = tc.post("/api/v1/knowledge-bases", json={"name": "Scoped KB", "code": "scoped-kb"}).json()
    login_as(tc, "admin", ADMIN["password"])
    foreign = tc.post("/api/v1/knowledge-bases", json={"name": "Foreign KB", "code": "foreign-kb"}).json()

    from app.db import SessionLocal
    with SessionLocal() as db:
        seed_document(db, scoped["id"], status="completed", title="inside")
        seed_document(db, scoped["id"], status="failed", error_code="RETRIEVAL_ENGINE_REJECTED", title="inside-failed")
        # mirrors DELETE /documents/{id}: sets deleted_at AND status="deleted"
        seed_document(db, scoped["id"], status="deleted", deleted=True, title="inside-deleted")
        seed_document(db, foreign["id"], status="completed", title="outside")
        seed_document(db, foreign["id"], status="failed", error_code="OCR_CHAIN_FAILED", title="outside-failed")
        seed_job(db, scoped["id"], None, status="queued")
        # error_breakdown reads ProcessingJob.error_code (the worker records the
        # terminal error on the job row), so failures need failed jobs seeded too
        failed_doc = seed_document(db, scoped["id"], status="failed", error_code="RETRIEVAL_ENGINE_REJECTED", title="inside-failed-job")
        seed_job(db, scoped["id"], failed_doc, status="failed", error_code="RETRIEVAL_ENGINE_REJECTED")
        foreign_doc = seed_document(db, foreign["id"], status="failed", error_code="OCR_CHAIN_FAILED", title="outside-failed-job")
        seed_job(db, foreign["id"], foreign_doc, status="failed", error_code="OCR_CHAIN_FAILED")
        seed_job(db, foreign["id"], None, status="running")
        seed_rejection(db, scoped["id"], "inside-rejected.pdf")
        seed_rejection(db, foreign["id"], "outside-rejected.pdf")
        db.commit()

    login_as(tc, "scopemgr")
    payload = tc.get("/api/v1/system/dashboard").json()
    metrics = payload["ingest_metrics"]

    # totals reflect ONLY the scoped KB
    assert metrics["total_documents"] == 3  # 1 completed + 2 failed, soft-deleted excluded
    assert metrics["completed_count"] == 1
    assert metrics["failed_count"] == 2
    assert metrics["in_queue_count"] == 1  # scoped queued job, not the foreign running one

    # KB name map never leaks the foreign tenant
    assert metrics["knowledge_bases"] == {scoped["id"]: "Scoped KB"}

    # rejections from the audit log are scoped by knowledge_base_id
    rejection_files = {r["filename"] for r in metrics["rejections"]}
    assert rejection_files == {"inside-rejected.pdf"}, rejection_files

    # error breakdown only counts the scoped failure
    breakdown = {row["error_code"]: row["count"] for row in metrics["error_breakdown"]}
    assert breakdown.get("RETRIEVAL_ENGINE_REJECTED") == 1
    assert "OCR_CHAIN_FAILED" not in breakdown


def test_dashboard_admin_sees_all_and_groups_degraded_status():
    tc = next(client())
    group = make_group(tc, "DashAdmin")
    make_user(tc, "adminowner", group_id=group["id"])

    login_as(tc, "adminowner")
    kb = tc.post("/api/v1/knowledge-bases", json={"name": "User KB", "code": "user-kb"}).json()
    login_as(tc, "admin", ADMIN["password"])
    other = tc.post("/api/v1/knowledge-bases", json={"name": "Admin KB", "code": "admin-kb"}).json()

    from app.db import SessionLocal
    with SessionLocal() as db:
        seed_document(db, kb["id"], status="completed")
        seed_document(db, other["id"], status="completed")
        db.commit()

    payload = tc.get("/api/v1/system/dashboard").json()
    assert payload["status"] in {"ready", "degraded"}  # database probe always runs
    assert payload["services"]["dependencies"]["database"] == "ready"
    metrics = payload["ingest_metrics"]
    assert metrics["total_documents"] == 2
    assert set(metrics["knowledge_bases"].values()) == {"User KB", "Admin KB"}


def test_dashboard_manager_with_no_visible_kbs_gets_zeros():
    tc = next(client())
    group = make_group(tc, "DashEmpty")
    make_user(tc, "emptymgr", role="manager", group_id=group["id"])
    # admin owns a KB with documents — the empty manager must see none of it
    login_as(tc, "admin", ADMIN["password"])
    kb = tc.post("/api/v1/knowledge-bases", json={"name": "AdminOnly KB", "code": "adminonly-kb"}).json()
    from app.db import SessionLocal
    with SessionLocal() as db:
        seed_document(db, kb["id"], status="completed")
        seed_rejection(db, kb["id"], "admin-only.pdf")
        db.commit()

    login_as(tc, "emptymgr")
    metrics = tc.get("/api/v1/system/dashboard").json()["ingest_metrics"]
    assert metrics["total_documents"] == 0
    assert metrics["knowledge_bases"] == {}
    assert metrics["rejections"] == []
    assert metrics["error_breakdown"] == []
