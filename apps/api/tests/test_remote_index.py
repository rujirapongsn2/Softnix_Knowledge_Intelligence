"""Ghost remote records: duplicate resolution, orphan repair, purge retries and error detail."""
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from datetime import datetime

from app import services
from app.models import Base, Document, KnowledgeBase, ProcessingJob
from app.remote_index import find_orphan_remote_documents, purge_orphan_remote_documents, resolve_remote_duplicate
from app.retrieval import LightRAGRetrievalEngine, RetrievalEngineError, duplicate_original_id, is_duplicate_content_error

DUPLICATE_ERROR = "Identical content already exists under another filename. Original doc_id: doc-ghost, Status: DocStatus.PROCESSED"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(KnowledgeBase(id="kb-1", name="KB", code="kb-r"))
    session.commit()
    yield session
    session.close()


def make_doc(db, doc_id, title="T", deleted=False, status="completed"):
    doc = Document(id=doc_id, knowledge_base_id="kb-1", original_filename=f"{doc_id}.md", stored_filename=f"{doc_id}.md",
                   storage_path=f"/tmp/{doc_id}.md", mime_type="text/markdown", file_size=1, checksum_sha256=doc_id.ljust(64, "0"),
                   title=title, status=status, extracted_text="hello world", deleted_at=datetime.utcnow() if deleted else None)
    db.add(doc)
    db.commit()
    return doc


class FakeEngine:
    enabled = True

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.deleted = []
        self.ingests = 0
        self.tracks = []

    def list_remote_documents(self):
        return [dict(row) for row in self.rows]

    def get_remote_document(self, remote_id):
        return next((dict(row) for row in self.rows if row["id"] == remote_id), None)

    def find_document(self, document_id, knowledge_base_id):
        return next((dict(row) for row in self.rows if row.get("document_id") == document_id), None)

    def delete_remote_document(self, remote_id):
        self.deleted.append(remote_id)
        self.rows = [row for row in self.rows if row["id"] != remote_id]

    def ingest(self, document_id, knowledge_base_id, text, title):
        self.ingests += 1
        return f"track-{self.ingests}"

    def track_status(self, track_id):
        return self.tracks.pop(0)


def row(remote_id, document_id, status="processed", title="Ghost"):
    return {"id": remote_id, "status": status, "knowledge_base_id": "kb-1", "document_id": document_id, "title": title}


def test_duplicate_error_helpers_and_classification():
    assert is_duplicate_content_error(DUPLICATE_ERROR) and is_duplicate_content_error("x [DUPLICATE:content_hash] y")
    assert duplicate_original_id(DUPLICATE_ERROR) == "doc-ghost" and duplicate_original_id("nothing") is None
    assert services.classify_track_failure(DUPLICATE_ERROR) == "RETRIEVAL_ENGINE_DUPLICATE"
    assert services.classify_track_failure("402 in_flight_budget_exhausted " + DUPLICATE_ERROR) == "RETRIEVAL_ENGINE_BUDGET_EXHAUSTED"


def test_stale_record_of_a_deleted_document_is_removed(db):
    make_doc(db, "new-doc")
    make_doc(db, "old-doc", deleted=True)
    engine = FakeEngine([row("doc-ghost", "old-doc"), row("dup-1", "new-doc", status="failed")])
    assert resolve_remote_duplicate(db, engine, db.get(Document, "new-doc"), DUPLICATE_ERROR) == "cleared"
    assert engine.deleted == ["doc-ghost", "dup-1"]


def test_stale_record_with_no_platform_row_is_removed(db):
    make_doc(db, "new-doc")
    engine = FakeEngine([row("doc-ghost", "missing-doc")])
    assert resolve_remote_duplicate(db, engine, db.get(Document, "new-doc"), DUPLICATE_ERROR) == "cleared"
    assert engine.deleted == ["doc-ghost"]


def test_live_twin_is_reported_not_deleted(db):
    make_doc(db, "new-doc")
    make_doc(db, "live-doc", title="Original act")
    engine = FakeEngine([row("doc-ghost", "live-doc")])
    with pytest.raises(RetrievalEngineError) as excinfo:
        resolve_remote_duplicate(db, engine, db.get(Document, "new-doc"), DUPLICATE_ERROR)
    assert str(excinfo.value) == "RETRIEVAL_ENGINE_DUPLICATE" and "Original act" in excinfo.value.detail
    assert engine.deleted == []


def test_unmanaged_or_unparseable_collisions_are_never_deleted(db):
    make_doc(db, "new-doc")
    foreign = FakeEngine([{"id": "doc-ghost", "status": "processed", "document_id": None}])
    with pytest.raises(RetrievalEngineError):
        resolve_remote_duplicate(db, foreign, db.get(Document, "new-doc"), DUPLICATE_ERROR)
    with pytest.raises(RetrievalEngineError):
        resolve_remote_duplicate(db, FakeEngine(), db.get(Document, "new-doc"), "Identical content already exists, no id")
    assert foreign.deleted == []


def test_already_indexed_by_the_same_document_is_idempotent(db):
    make_doc(db, "new-doc")
    engine = FakeEngine([row("doc-ghost", "new-doc")])
    assert resolve_remote_duplicate(db, engine, db.get(Document, "new-doc"), DUPLICATE_ERROR) == "already_indexed"
    assert engine.deleted == []


def run_job(db, monkeypatch, engine, doc_id="new-doc"):
    monkeypatch.setattr(services, "LightRAGRetrievalEngine", lambda: engine)
    monkeypatch.setattr(services.time, "sleep", lambda _: None)
    monkeypatch.setattr(services, "extract_text", lambda document: document.extracted_text)
    monkeypatch.setattr(services, "embed_document_chunks", lambda *a, **k: None)
    monkeypatch.setattr(services, "sync_lightrag_document_graph", lambda *a, **k: None)
    db.add(ProcessingJob(document_id=doc_id, knowledge_base_id="kb-1"))
    db.commit()
    assert services.process_next_job(db) is True
    db.expire_all()
    return db.get(Document, doc_id), db.query(ProcessingJob).filter_by(document_id=doc_id, job_type="PROCESS_DOCUMENT").one()


def test_worker_clears_a_ghost_and_ingests_again(db, monkeypatch):
    make_doc(db, "new-doc", status="queued")
    make_doc(db, "old-doc", deleted=True)
    engine = FakeEngine([row("doc-ghost", "old-doc")])
    engine.tracks = [{"status": "failed", "error": DUPLICATE_ERROR}, {"status": "processed", "error": None}]
    doc, job = run_job(db, monkeypatch, engine)
    assert (doc.status, job.status, engine.ingests, engine.deleted) == ("completed", "completed", 2, ["doc-ghost"])


def test_worker_reports_a_live_twin_with_a_clear_message(db, monkeypatch):
    make_doc(db, "new-doc", status="queued")
    make_doc(db, "live-doc", title="Original act")
    engine = FakeEngine([row("doc-ghost", "live-doc")])
    engine.tracks = [{"status": "failed", "error": DUPLICATE_ERROR}]
    doc, job = run_job(db, monkeypatch, engine)
    assert (doc.status, doc.error_code, job.status, job.attempt_count) == ("failed", "RETRIEVAL_ENGINE_DUPLICATE", "failed", 1)
    assert "Original act" in job.error_message and "Identical content" in doc.error_message


def test_worker_keeps_the_engine_detail_for_other_rejections(db, monkeypatch):
    make_doc(db, "new-doc", status="queued")
    engine = FakeEngine()
    engine.tracks = [{"status": "failed", "error": "Regex error while tokenizing"}] * 3
    doc, job = run_job(db, monkeypatch, engine)
    assert job.error_code == "RETRIEVAL_ENGINE_REJECTED" and "Regex error while tokenizing" in job.error_message


def test_orphan_report_and_repair(db):
    make_doc(db, "live-doc")
    make_doc(db, "gone-doc", deleted=True)
    engine = FakeEngine([row("a", "live-doc"), row("b", "gone-doc"), row("c", "missing-doc", status="failed"),
                         row("d", "gone-doc", status="processing"), {"id": "e", "status": "processed", "document_id": None}])
    assert {orphan.remote_id for orphan in find_orphan_remote_documents(db, engine)} == {"b", "c"}
    assert purge_orphan_remote_documents(db, engine)["removed"] == [] and engine.deleted == []
    report = purge_orphan_remote_documents(db, engine, apply=True)
    assert sorted(report["removed"]) == ["b", "c"] and report["failed"] == []


def test_orphan_repair_continues_after_a_busy_record(db):
    make_doc(db, "gone-doc", deleted=True)
    engine = FakeEngine([row("a", "gone-doc"), row("b", "gone-doc")])
    original = engine.delete_remote_document

    def flaky(remote_id):
        if remote_id == "a":
            raise RetrievalEngineError("RETRIEVAL_ENGINE_BUSY")
        original(remote_id)

    engine.delete_remote_document = flaky
    report = purge_orphan_remote_documents(db, engine, apply=True, busy_retries=1, busy_wait=0)
    assert report["removed"] == ["b"] and report["failed"] == [{"remote_id": "a", "error": "RETRIEVAL_ENGINE_BUSY"}]


def test_purge_job_retries_when_the_engine_is_unavailable(db, monkeypatch):
    make_doc(db, "gone-doc", deleted=True)

    class Down:
        enabled = True

        def find_document(self, *a, **k):
            raise RetrievalEngineError("RETRIEVAL_ENGINE_UNAVAILABLE", "ConnectError")

    monkeypatch.setattr(services, "LightRAGRetrievalEngine", lambda: Down())
    db.add(ProcessingJob(document_id="gone-doc", knowledge_base_id="kb-1", job_type="PURGE_REMOTE_INDEX"))
    db.commit()
    assert services.process_next_job(db) is True
    job = db.query(ProcessingJob).filter_by(job_type="PURGE_REMOTE_INDEX").one()
    assert (job.status, job.error_code) == ("queued", "RETRIEVAL_ENGINE_UNAVAILABLE") and job.next_attempt_at > datetime.utcnow()


def test_engine_http_errors_carry_status_and_body():
    def handler(request):
        return httpx.Response(422, text="text too short") if request.url.path == "/documents/text" else httpx.Response(409, text="busy")

    engine = LightRAGRetrievalEngine(base_url="http://lightrag", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RetrievalEngineError) as rejected:
        engine._request("POST", "/documents/text", json={})
    assert str(rejected.value) == "RETRIEVAL_ENGINE_REJECTED" and "HTTP 422" in rejected.value.detail and "text too short" in rejected.value.detail
    with pytest.raises(RuntimeError, match="RETRIEVAL_ENGINE_BUSY"):
        engine._request("GET", "/documents")


def test_orphan_repair_waits_out_a_busy_engine(db):
    make_doc(db, "gone-doc", deleted=True)
    engine = FakeEngine([row("a", "gone-doc")])
    original, attempts = engine.delete_remote_document, []

    def busy_twice(remote_id):
        attempts.append(remote_id)
        if len(attempts) < 3:
            raise RetrievalEngineError("RETRIEVAL_ENGINE_BUSY")
        original(remote_id)

    engine.delete_remote_document = busy_twice
    report = purge_orphan_remote_documents(db, engine, apply=True, busy_wait=0)
    assert report["removed"] == ["a"] and report["failed"] == [] and len(attempts) == 3


def test_orphan_sweep_ignores_records_of_unknown_knowledge_bases(db):
    make_doc(db, "gone-doc", deleted=True)
    engine = FakeEngine([row("mine", "gone-doc"), {**row("foreign", "other-env-doc"), "knowledge_base_id": "kb-from-another-environment"}])
    assert [orphan.remote_id for orphan in find_orphan_remote_documents(db, engine)] == ["mine"]


def test_stale_record_already_removed_by_a_purge_job_is_not_an_error(db):
    make_doc(db, "new-doc")
    make_doc(db, "old-doc", deleted=True)
    engine = FakeEngine([row("doc-ghost", "old-doc")])

    def purge_won_the_race(remote_id):
        engine.rows = []
        raise RetrievalEngineError("RETRIEVAL_ENGINE_REJECTED", "HTTP 404")

    engine.delete_remote_document = purge_won_the_race
    assert resolve_remote_duplicate(db, engine, db.get(Document, "new-doc"), DUPLICATE_ERROR) == "cleared"


def test_busy_delete_still_propagates_so_the_job_retries(db):
    make_doc(db, "new-doc")
    make_doc(db, "old-doc", deleted=True)
    engine = FakeEngine([row("doc-ghost", "old-doc")])

    def busy(remote_id):
        raise RetrievalEngineError("RETRIEVAL_ENGINE_BUSY")

    engine.delete_remote_document = busy
    with pytest.raises(RuntimeError, match="RETRIEVAL_ENGINE_BUSY"):
        resolve_remote_duplicate(db, engine, db.get(Document, "new-doc"), DUPLICATE_ERROR)


def test_worker_survives_a_reingest_that_returns_no_track(db, monkeypatch):
    make_doc(db, "new-doc", status="queued")
    make_doc(db, "old-doc", deleted=True)
    engine = FakeEngine([row("doc-ghost", "old-doc")])
    engine.tracks = [{"status": "failed", "error": DUPLICATE_ERROR}]
    calls = []

    def first_ingest_has_a_track_the_second_does_not(*args, **kwargs):
        calls.append(1)
        return "track-1" if len(calls) == 1 else None

    engine.ingest = first_ingest_has_a_track_the_second_does_not
    doc, job = run_job(db, monkeypatch, engine)
    assert (doc.status, job.status, len(calls)) == ("completed", "completed", 2)


# --- engine outages: long retries and recovery sweep ----------------------------

from datetime import timedelta

from app.remote_index import MAX_AUTO_RECOVERIES, RECOVERY_JOB_TYPE, recover_unavailable_documents


def test_unavailable_retry_delay_backs_off_to_five_minutes():
    assert [services.engine_unavailable_retry_delay(n) for n in range(1, 9)] == [30, 60, 120, 240, 300, 300, 300, 300]


class DownEngine(FakeEngine):
    def ingest(self, *args, **kwargs):
        raise RetrievalEngineError("RETRIEVAL_ENGINE_UNAVAILABLE", "ConnectError")


def test_worker_keeps_retrying_while_the_engine_is_unreachable_then_gives_up(db, monkeypatch):
    make_doc(db, "new-doc", status="queued")
    monkeypatch.setattr(services, "LightRAGRetrievalEngine", lambda: DownEngine())
    monkeypatch.setattr(services, "extract_text", lambda document: document.extracted_text)
    monkeypatch.setattr(services, "embed_document_chunks", lambda *a, **k: None)
    job = ProcessingJob(document_id="new-doc", knowledge_base_id="kb-1")
    db.add(job)
    db.commit()
    for attempt in range(1, services.MAX_ENGINE_UNAVAILABLE_ATTEMPTS + 1):
        job.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
        assert services.process_next_job(db) is True
        db.refresh(job)
        if attempt < services.MAX_ENGINE_UNAVAILABLE_ATTEMPTS:
            wait = (job.next_attempt_at - datetime.utcnow()).total_seconds()
            assert job.status == "queued" and abs(wait - services.engine_unavailable_retry_delay(attempt)) < 5
    assert (job.status, job.attempt_count, job.error_code) == ("failed", services.MAX_ENGINE_UNAVAILABLE_ATTEMPTS, "RETRIEVAL_ENGINE_UNAVAILABLE")
    assert db.get(Document, "new-doc").status == "failed"


class HealthEngine:
    def __init__(self, healthy):
        self.healthy = healthy

    def is_healthy(self):
        return self.healthy


def failed_doc(db, doc_id, code="RETRIEVAL_ENGINE_UNAVAILABLE", **kwargs):
    doc = make_doc(db, doc_id, status="failed", **kwargs)
    doc.error_code = code
    db.commit()
    return doc


def test_recovery_requeues_documents_that_failed_only_because_the_engine_was_down(db):
    failed_doc(db, "down-1")
    failed_doc(db, "other-error", code="RETRIEVAL_ENGINE_REJECTED")
    failed_doc(db, "deleted", deleted=True)
    assert recover_unavailable_documents(db, HealthEngine(False)) == 0
    assert recover_unavailable_documents(db, HealthEngine(True)) == 1
    doc = db.get(Document, "down-1")
    assert (doc.status, doc.error_code) == ("queued", None)
    assert [j.job_type for j in db.query(ProcessingJob).filter_by(document_id="down-1")] == [RECOVERY_JOB_TYPE]
    assert db.get(Document, "other-error").status == "failed" and db.get(Document, "deleted").status == "failed"
    assert recover_unavailable_documents(db, HealthEngine(True)) == 0  # nothing left to re-queue


def test_recovery_is_bounded_per_sweep_per_document_and_by_age(db):
    for index in range(7):
        failed_doc(db, f"down-{index}")
    assert recover_unavailable_documents(db, HealthEngine(True), limit=5) == 5
    failed_doc(db, "stale")
    db.query(Document).filter_by(id="stale").update({"updated_at": datetime.utcnow() - timedelta(days=60)})
    db.commit()
    assert recover_unavailable_documents(db, HealthEngine(True), limit=50) == 2 and db.get(Document, "stale").status == "failed"
    # a document that keeps failing stops being re-queued after MAX_AUTO_RECOVERIES
    for _ in range(MAX_AUTO_RECOVERIES):
        db.add(ProcessingJob(document_id="stale", knowledge_base_id="kb-1", job_type=RECOVERY_JOB_TYPE, status="failed"))
    db.query(Document).filter_by(id="stale").update({"updated_at": datetime.utcnow()})
    db.commit()
    assert recover_unavailable_documents(db, HealthEngine(True)) == 0


def test_recovery_skips_documents_that_already_have_work_in_flight(db):
    failed_doc(db, "busy-doc")
    db.add(ProcessingJob(document_id="busy-doc", knowledge_base_id="kb-1", status="queued"))
    db.commit()
    assert recover_unavailable_documents(db, HealthEngine(True)) == 0


def test_engine_health_probe():
    def make(status, body):
        transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
        return LightRAGRetrievalEngine(base_url="http://lightrag", client=httpx.Client(transport=transport))

    assert make(200, {"status": "healthy"}).is_healthy() is True
    assert make(200, {"status": "starting"}).is_healthy() is False
    assert make(503, {}).is_healthy() is False
    assert LightRAGRetrievalEngine(base_url="").is_healthy() is False
