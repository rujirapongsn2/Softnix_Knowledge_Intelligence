"""Regression tests for the E2E round-2 findings (F1–F6).

Self-contained SQLite + TestClient bootstrap, following test_rbac.py /
test_system_dashboard.py conventions: environment first, app imports after.
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

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

import app.services as services  # noqa: E402
from app.retrieval import LightRAGRetrievalEngine  # noqa: E402

ADMIN = {"username": "admin", "password": "correct-horse-battery-staple"}


@pytest.fixture(autouse=True)
def _fresh_db():
    from app.db import Base, SessionLocal, engine
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        from app.config import get_settings
        from app.security import password_hash
        from app.models import User
        settings = get_settings()
        db.add(User(username=settings.initial_admin_username,
                    password_hash=password_hash(settings.initial_admin_password),
                    role="admin"))
        db.commit()
    yield
    Base.metadata.drop_all(engine)


# ---------------------------------------------------------------- F5
def test_source_label_sanitizes_slash_in_title():
    label = LightRAGRetrievalEngine._source_label("kb-1", "doc-1", "RFC 2616 HTTP/1.1")
    assert "/" not in label
    assert label == "softnix-kb=kb-1__doc=doc-1__RFC 2616 HTTP_1.1"


def test_decode_recovers_label_even_after_basename_truncation():
    full = "softnix-kb=kb-1__doc=doc-1__RFC 2616 HTTP/1.1"
    assert LightRAGRetrievalEngine._decode_source_label(full) == ("kb-1", "doc-1", "RFC 2616 HTTP/1.1")
    # A basename truncated at the title slash carries no marker: unrecoverable.
    assert LightRAGRetrievalEngine._decode_source_label("skip/kb-1/1.1") is None


def test_decode_ignores_foreign_labels():
    assert LightRAGRetrievalEngine._decode_source_label("some/other/file.txt") is None
    assert LightRAGRetrievalEngine._decode_source_label("") is None


def test_ingest_file_source_survives_remote_normalisation():
    seen = {}

    def handler(request: httpx.Request):
        if request.url.path == "/documents/text":
            seen["file_source"] = json.loads(request.content)["file_source"]
            return httpx.Response(202, json={"track_id": "track-1"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    engine = LightRAGRetrievalEngine(base_url="http://lightrag.test", api_key="key",
                                     client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert engine.ingest("doc-1", "kb-1", "text", "RFC 2616 HTTP/1.1") == "track-1"
    assert seen["file_source"] == "skip/kb-1/softnix-kb=kb-1__doc=doc-1__RFC 2616 HTTP_1.1"


# ---------------------------------------------------------------- F1
def test_track_status_returns_error_detail():
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"documents": [
            {"status": "FAILED", "error_msg": "openrouter 402 in_flight_budget_exhausted"},
        ]})

    engine = LightRAGRetrievalEngine(base_url="http://lightrag.test", client=httpx.Client(transport=httpx.MockTransport(handler)))
    track = engine.track_status("track-1")
    assert track["status"] == "failed"
    assert "in_flight_budget_exhausted" in track["error"]


def test_classify_track_failure_maps_budget_markers():
    assert services.classify_track_failure("402 in_flight_budget_exhausted") == "RETRIEVAL_ENGINE_BUDGET_EXHAUSTED"
    assert services.classify_track_failure("Provider returned 402") == "RETRIEVAL_ENGINE_BUDGET_EXHAUSTED"
    assert services.classify_track_failure("some parse error") == "RETRIEVAL_ENGINE_REJECTED"
    assert services.classify_track_failure(None) == "RETRIEVAL_ENGINE_REJECTED"


def test_budget_error_is_transient_with_longer_backoff():
    assert "RETRIEVAL_ENGINE_BUDGET_EXHAUSTED" in services.TRANSIENT_PROCESSING_ERRORS
    assert services.MAX_BUDGET_PROCESSING_ATTEMPTS > services.MAX_PROCESSING_ATTEMPTS
    assert services.BUDGET_RETRY_DELAY_FLOOR_SECONDS >= services.processing_retry_delay(services.MAX_PROCESSING_ATTEMPTS)


def test_indexing_semaphore_limits_concurrency():
    assert services.INDEXING_CONCURRENCY_LIMIT == 2
    assert services._indexing_semaphore._value == services.INDEXING_CONCURRENCY_LIMIT


def test_worker_requeues_budget_failure_with_next_attempt():
    """The worker loop retries budget failures instead of failing the job."""
    from datetime import datetime

    from app.db import SessionLocal
    from app.models import Document, KnowledgeBase, ProcessingJob

    db = SessionLocal()
    try:
        kb = KnowledgeBase(name="KB", code="kb-f1")
        db.add(kb)
        db.commit()
        doc = Document(knowledge_base_id=kb.id, title="doc", original_filename="doc.txt",
                       stored_filename="doc.txt", storage_path=f"{_TEST_ROOT}/doc.txt",
                       mime_type="text/plain", file_size=4, checksum_sha256="a" * 64,
                       status="queued")
        Path(doc.storage_path).write_text("body")
        db.add(doc)
        db.commit()
        job = ProcessingJob(document_id=doc.id, knowledge_base_id=kb.id, job_type="PROCESS_DOCUMENT")
        db.add(job)
        db.commit()
        job_id, doc_id = job.id, doc.id

        class FakeEngine:
            enabled = True

            def ingest(self, *args, **kwargs):
                return "track-1"

            def track_status(self, track_id):
                return {"status": "failed", "error": "402 in_flight_budget_exhausted"}

        original = services.LightRAGRetrievalEngine
        services.LightRAGRetrievalEngine = lambda: FakeEngine()
        try:
            assert services.process_next_job(db) is True
        finally:
            services.LightRAGRetrievalEngine = original

        db.rollback()
        job = db.get(ProcessingJob, job_id)
        doc = db.get(Document, doc_id)
        assert job is not None and doc is not None
        assert job.status == "queued"
        assert job.current_stage == "retry_wait"
        assert job.error_code == "RETRIEVAL_ENGINE_BUDGET_EXHAUSTED"
        assert doc.status == "queued"
        assert job.next_attempt_at > datetime.utcnow()
    finally:
        db.close()


# ---------------------------------------------------------------- F2
def test_completed_document_clears_stale_error_code():
    from app.db import SessionLocal
    from app.models import Document, KnowledgeBase, ProcessingJob

    db = SessionLocal()
    try:
        kb = KnowledgeBase(name="KB", code="kb-f2")
        db.add(kb)
        db.commit()
        doc = Document(knowledge_base_id=kb.id, title="doc", original_filename="doc.txt",
                       stored_filename="doc2.txt", storage_path=f"{_TEST_ROOT}/doc2.txt",
                       mime_type="text/plain", file_size=4, checksum_sha256="b" * 64,
                       status="queued",
                       error_code="RETRIEVAL_ENGINE_REJECTED", error_message="old failure")
        Path(doc.storage_path).write_text("body")
        db.add(doc)
        db.commit()
        job = ProcessingJob(document_id=doc.id, knowledge_base_id=kb.id, job_type="PROCESS_DOCUMENT")
        db.add(job)
        db.commit()
        job_id, doc_id = job.id, doc.id

        class FakeEngine:
            enabled = False

        original = services.LightRAGRetrievalEngine
        services.LightRAGRetrievalEngine = lambda: FakeEngine()
        try:
            assert services.process_next_job(db) is True
        finally:
            services.LightRAGRetrievalEngine = original

        db.rollback()
        job = db.get(ProcessingJob, job_id)
        doc = db.get(Document, doc_id)
        assert job is not None and doc is not None
        assert job.status == "completed"
        assert doc.status == "completed"
        assert doc.error_code is None
        assert doc.error_message is None
    finally:
        db.close()


# ---------------------------------------------------------------- F6
def _instrument(status):
    from types import SimpleNamespace

    return SimpleNamespace(
        status=status, official_title="NIST CSF", authority_level=40, kind="framework",
        version_label=None, effective_from=None, effective_to=None, version_date=None,
        legal_work_key=None, document_class="framework", version_role=None,
        source_uri=None, source_reference=None, review_status="pending", document_id="doc-1",
    )


def test_unknown_status_is_not_a_hard_gate():
    source = {"document_id": "doc-1", "title": "NIST CSF"}
    services._decorate_sources_with_legal_metadata([source], {"doc-1": _instrument("unknown")})
    assert "ไม่ทราบสถานะ" not in (source["legal_label"] or "")
    assert "สถานะ:" not in (source["legal_label"] or "")


def test_known_adverse_status_still_labels_the_source():
    source = {"document_id": "doc-1", "title": "Old Act"}
    services._decorate_sources_with_legal_metadata([source], {"doc-1": _instrument("repealed")})
    assert "ถูกยกเลิก" in source["legal_label"]


# ---------------------------------------------------------------- F3
def test_html_flavored_xls_extraction():
    html = """<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:office:excel">
<head><meta charset="utf-8"></head>
<body><table>
<tr><th>เดือน</th><th>ปริมาณน้ำฝน (มม.)</th></tr>
<tr><td>มกราคม</td><td>12.5</td></tr>
<tr><td>กุมภาพันธ์</td><td>8.3</td></tr>
</table></body></html>"""
    path = Path(_TEST_ROOT) / "gov.xls"
    path.write_text(html, encoding="utf-8")
    text = services._extract_html_flavored_xls(path)
    assert "มกราคม" in text and "12.5" in text
    assert " | " in text  # table structure preserved


def test_binary_xls_is_not_html_parsed():
    path = Path(_TEST_ROOT) / "real.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1binaryjunk")
    with pytest.raises(RuntimeError) as excinfo:
        services._extract_html_flavored_xls(path)
    assert str(excinfo.value) == "FILE_TYPE_NOT_SUPPORTED"


def test_anydoc_malformed_error_falls_through_to_html_fallback(monkeypatch):
    """MalformedError (not RuntimeError) must reach the HTML fallback (F3)."""
    import app.doc_extraction as doc_extraction

    class MalformedError(Exception):
        pass

    def fake_extract(document):
        raise MalformedError("malformed document: unreadable workbook")

    monkeypatch.setattr(doc_extraction, "extract_document_text", fake_extract, raising=False)

    html = "<html><body><table><tr><td>ทดสอบ</td></tr></table></body></html>"
    path = Path(_TEST_ROOT) / "flav.xls"
    path.write_text(html, encoding="utf-8")

    class Doc:
        storage_path = str(path)

    text = services.extract_text(Doc())
    assert "ทดสอบ" in text


# ---------------------------------------------------------------- F4
def test_follow_up_jobs_take_priority():
    from app.db import SessionLocal
    from app.models import Document, KnowledgeBase, ProcessingJob

    db = SessionLocal()
    try:
        kb = KnowledgeBase(name="KB", code="kb-f4")
        db.add(kb)
        db.commit()
        doc = Document(knowledge_base_id=kb.id, title="doc", original_filename="doc.txt",
                       stored_filename="doc4.txt", storage_path=f"{_TEST_ROOT}/doc4.txt",
                       mime_type="text/plain", file_size=4, checksum_sha256="c" * 64)
        Path(doc.storage_path).write_text("body")
        db.add(doc)
        db.commit()
        early_process = ProcessingJob(document_id=doc.id, knowledge_base_id=kb.id, job_type="PROCESS_DOCUMENT")
        later_followup = ProcessingJob(document_id=doc.id, knowledge_base_id=kb.id, job_type="EXTRACT_LEGAL_METADATA")
        db.add_all([early_process, later_followup])
        db.commit()

        picked = db.query(ProcessingJob).filter(
            ProcessingJob.status == "queued",
            ProcessingJob.job_type.in_(services._FOLLOW_UP_JOB_TYPES),
        ).order_by(ProcessingJob.created_at).first()
        assert picked is not None
        assert picked.id == later_followup.id  # follow-up wins despite later created_at
    finally:
        db.close()
