"""Code-review fixes for the job queue (SQLite path; PostgreSQL locking in test_job_queue_postgres.py)."""
import time
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import services, worker
from app.db import Base, SessionLocal, engine
from app.job_queue import GRAPH_EVENT_MAX_ATTEMPTS, WORKER_LOST, ClaimLost, claim_next_job, disown, own_job, sweep_stale_jobs
from app.main import app
from app.models import Document, GraphProjectionEvent, JobType, KnowledgeBase, ProcessingJob
from app.retry_policy import PROCESSING_RETRY_POLICIES, PURGE_MAX_BUSY_DEFERRALS, purge_busy_delay


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        # Other modules share this database: park their jobs so claims only see ours.
        db.query(ProcessingJob).filter(ProcessingJob.status.in_(["queued", "running"])).update({"status": "cancelled"}, synchronize_session=False)
        db.commit()


def _kb(db):
    kb = KnowledgeBase(name="Review KB", code=f"review-{uuid.uuid4().hex[:10]}", status="active")
    db.add(kb); db.commit()
    return kb


def _doc(db, kb, deleted=False):
    doc = Document(knowledge_base_id=kb.id, original_filename="r.txt", stored_filename=f"{uuid.uuid4()}.txt",
                   storage_path="/tmp/does-not-matter.txt", mime_type="text/plain", file_size=1,
                   checksum_sha256=uuid.uuid4().hex * 2, title="r", tags=[], status="queued",
                   deleted_at=datetime.utcnow() if deleted else None)
    db.add(doc); db.commit()
    return doc


def _job(db, doc, job_type=JobType.PROCESS_DOCUMENT):
    job = ProcessingJob(document_id=doc.id, knowledge_base_id=doc.knowledge_base_id, job_type=job_type)
    db.add(job); db.commit()
    return job


def _login(client):
    assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200


# ------------------------------------------------------------ Finding 5
@pytest.mark.parametrize("code", ["LLM_REQUEST_REJECTED"])
def test_a_rejected_request_fails_fast_without_retry(monkeypatch, code):
    assert PROCESSING_RETRY_POLICIES.get(code) is None
    with SessionLocal() as db:
        doc = _doc(db, _kb(db)); job = _job(db, doc)
        monkeypatch.setattr(services, "extract_text", lambda _doc: "body")

        def rejected(_db, _id):
            raise RuntimeError(code)

        monkeypatch.setattr(services, "embed_document_chunks", rejected)
        assert services.process_next_job(db) is True
        db.expire_all()
        job, doc = db.get(ProcessingJob, job.id), db.get(Document, doc.id)
        assert job.status == "failed" and job.error_code == code and job.attempt_count == 1
        assert doc.status == "failed" and doc.error_code == code


@pytest.mark.parametrize("code", ["LLM_CREDITS_EXHAUSTED", "LLM_AUTH_FAILED"])
def test_exhausted_credits_or_a_rejected_key_wait_for_the_provider(monkeypatch, code):
    assert PROCESSING_RETRY_POLICIES.get(code) is None, "provider_holds handles these, not the retry schedule"
    with SessionLocal() as db:
        doc = _doc(db, _kb(db)); job = _job(db, doc)
        monkeypatch.setattr(services, "extract_text", lambda _doc: "body")
        monkeypatch.setattr(services, "embed_document_chunks", lambda *_: (_ for _ in ()).throw(RuntimeError(code)))
        assert services.process_next_job(db) is True
        db.expire_all()
        job = db.get(ProcessingJob, job.id)
        assert (job.status, job.current_stage, job.attempt_count) == ("queued", "waiting_for_provider", 0)


# ------------------------------------------------------------ Finding 3
def test_delete_during_indexing_waits_for_the_ingest_then_purges(monkeypatch):
    with TestClient(app) as client:
        _login(client)
        with SessionLocal() as db:
            doc = _doc(db, _kb(db)); job = _job(db, doc)
            doc_id, job_id = doc.id, job.id
        observed = {}
        calls = []

        class Engine:
            enabled = True

        def index_in_engine(_db, _engine, _doc, _text, _job):
            calls.append("ingest")
            # The user deletes the document while the engine ingests it.
            assert client.delete(f"/api/v1/documents/{doc_id}").status_code in (200, 202, 204)
            with SessionLocal() as other:
                observed["job"] = other.get(ProcessingJob, job_id).status
                observed["purge"] = other.query(ProcessingJob).filter_by(document_id=doc_id, job_type=JobType.PURGE_REMOTE_INDEX).one().status
                # One running job per document: the purge waits for the ingest.
                observed["claimable"] = claim_next_job(other)

        monkeypatch.setattr(services, "extract_text", lambda _doc: "body")
        monkeypatch.setattr(services, "embed_document_chunks", lambda *_: None)
        monkeypatch.setattr(services, "LightRAGRetrievalEngine", lambda: Engine())
        monkeypatch.setattr(services, "index_in_engine", index_in_engine)
        monkeypatch.setattr(services, "sync_lightrag_document_graph", lambda *_: calls.append("graph-sync"))
        with SessionLocal() as db:
            assert services.process_next_job(db) is True
            assert observed == {"job": "running", "purge": "queued", "claimable": None}
            assert calls == ["ingest"], "nothing is written for a deleted document after the ingest"
            db.expire_all()
            job = db.get(ProcessingJob, job_id)
            assert job.status == "cancelled" and job.error_code == "DOCUMENT_DELETED"
            assert db.get(Document, doc_id).status == "deleted"
            # Now the purge runs and removes the finished remote record.
            purge = claim_next_job(db)
            assert purge.job_type == JobType.PURGE_REMOTE_INDEX and purge.document_id == doc_id


def test_delete_during_legal_extraction_writes_no_registry_facts(monkeypatch):
    with TestClient(app) as client:
        _login(client)
        with SessionLocal() as db:
            doc = _doc(db, _kb(db)); job = _job(db, doc, JobType.EXTRACT_LEGAL_METADATA)
            doc_id, job_id = doc.id, job.id
        writes = []

        def parse(_text, _title):
            # The user deletes the document while the legal header is being read.
            assert client.delete(f"/api/v1/documents/{doc_id}").status_code < 300
            return {"instrument": {"version_role": "consolidated"}, "change_events": [], "amendments": [], "provenance": {}}

        monkeypatch.setattr(services, "extract_text", lambda _doc: "body")
        monkeypatch.setattr(services, "parse_legal_corpus_metadata", parse)
        for name in ("sync_legal_document_graph", "upsert_legal_instrument", "link_provisions_to_chunks", "resolve_instrument_statuses"):
            monkeypatch.setattr(services, name, lambda *_a, _name=name, **_k: writes.append(_name))
        with SessionLocal() as db:
            assert services.process_next_job(db) is True
            db.expire_all()
            job = db.get(ProcessingJob, job_id)
            assert (job.status, job.error_code) == ("cancelled", "DOCUMENT_DELETED")
            assert writes == [], "legal registry and graph facts were written for a deleted document"
            assert db.get(Document, doc_id).legal_metadata is None


def test_delete_cancels_queued_jobs_and_releases_a_stale_running_one():
    with TestClient(app) as client:
        _login(client)
        with SessionLocal() as db:
            kb = _kb(db)
            queued_doc, stale_doc = _doc(db, kb), _doc(db, kb)
            queued = _job(db, queued_doc)
            stale = ProcessingJob(document_id=stale_doc.id, knowledge_base_id=kb.id, job_type=JobType.PROCESS_DOCUMENT,
                                  status="running", claim_token="dead-worker")
            db.add(stale); db.commit()
            db.query(ProcessingJob).filter_by(id=stale.id).update({"updated_at": datetime.utcnow() - timedelta(hours=1)}, synchronize_session=False)
            db.commit()
            ids = queued_doc.id, stale_doc.id, queued.id, stale.id
        assert client.delete(f"/api/v1/documents/{ids[0]}").status_code < 300
        assert client.delete(f"/api/v1/documents/{ids[1]}").status_code < 300
        with SessionLocal() as db:
            assert db.get(ProcessingJob, ids[2]).status == "cancelled"
            released = db.get(ProcessingJob, ids[3])
            assert released.status == "cancelled" and released.claim_token is None
            # The same hand-over as the sweep: the loss is counted and the reason recorded.
            assert (released.lost_count, released.error_code) == (1, "DOCUMENT_DELETED")


# ------------------------------------------------------------ Finding 4
def test_busy_purge_is_postponed_without_using_an_attempt(monkeypatch):
    class Engine:
        enabled = True

        def find_document(self, *_a, **_k):
            return {"id": "remote"}

        def delete_remote_document(self, *_a, **_k):
            raise RuntimeError("RETRIEVAL_ENGINE_BUSY")

    monkeypatch.setattr(services, "LightRAGRetrievalEngine", lambda: Engine())
    with SessionLocal() as db:
        doc = _doc(db, _kb(db), deleted=True)
        job = _job(db, doc, JobType.PURGE_REMOTE_INDEX)
        delays = []
        for _ in range(8):
            db.query(ProcessingJob).filter_by(id=job.id).update({"next_attempt_at": datetime.utcnow() - timedelta(seconds=1)}, synchronize_session=False)
            db.commit()
            before = datetime.utcnow()
            assert services.process_next_job(db) is True
            db.expire_all()
            job = db.get(ProcessingJob, job.id)
            assert job.status == "queued" and job.error_code == "RETRIEVAL_ENGINE_BUSY"
            delays.append(round((job.next_attempt_at - before).total_seconds()))
        assert job.attempt_count == 0 and job.deferral_count == 8
        assert delays == [15, 30, 60, 120, 240, 300, 300, 300]
        # Busy for many hours (PURGE_MAX_BUSY_DEFERRALS) finally counts as failures.
        assert sum(purge_busy_delay(n) for n in range(1, PURGE_MAX_BUSY_DEFERRALS + 1)) > 12 * 3600
        db.query(ProcessingJob).filter_by(id=job.id).update({"next_attempt_at": datetime.utcnow() - timedelta(seconds=1), "deferral_count": PURGE_MAX_BUSY_DEFERRALS}, synchronize_session=False)
        db.commit()
        assert services.process_next_job(db) is True
        db.expire_all()
        assert db.get(ProcessingJob, job.id).attempt_count == 1


def test_a_purge_that_waited_out_busy_completes_clean(monkeypatch):
    class Engine:
        enabled = True

        def find_document(self, *_a, **_k):
            return None

    monkeypatch.setattr(services, "LightRAGRetrievalEngine", lambda: Engine())
    with SessionLocal() as db:
        job = _job(db, _doc(db, _kb(db), deleted=True), JobType.PURGE_REMOTE_INDEX)
        job.error_code, job.deferral_count = "RETRIEVAL_ENGINE_BUSY", 3
        db.commit()
        assert services.process_next_job(db) is True
        db.expire_all()
        job = db.get(ProcessingJob, job.id)
        assert job.status == "completed" and job.error_code is None


def test_purges_run_one_at_a_time():
    with SessionLocal() as db:
        kb = _kb(db)
        first = _job(db, _doc(db, kb, deleted=True), JobType.PURGE_REMOTE_INDEX)
        second = _job(db, _doc(db, kb, deleted=True), JobType.PURGE_REMOTE_INDEX)
        assert claim_next_job(db).id == first.id
        assert claim_next_job(db) is None
        db.query(ProcessingJob).filter_by(id=first.id).update({"status": "completed"}, synchronize_session=False)
        db.commit()
        assert claim_next_job(db).id == second.id


# ------------------------------------------------------------ Finding 9
def test_a_superseded_worker_cannot_write_results(monkeypatch):
    with SessionLocal() as db:
        doc = _doc(db, _kb(db)); job = _job(db, doc)
        doc_id, job_id = doc.id, job.id

    def extract(_doc):
        # The job is swept / replaced by a reprocess while this worker extracts.
        with SessionLocal() as other:
            stolen = other.get(ProcessingJob, job_id)
            disown(stolen)
            stolen.status, stolen.error_code = "failed", WORKER_LOST
            other.commit()
        return "late text"

    monkeypatch.setattr(services, "extract_text", extract)
    monkeypatch.setattr(services, "embed_document_chunks", lambda *_: pytest.fail("a superseded worker must stop"))
    with SessionLocal() as db:
        assert services.process_next_job(db) is True
        db.expire_all()
        job, doc = db.get(ProcessingJob, job_id), db.get(Document, doc_id)
        assert job.status == "failed" and job.error_code == WORKER_LOST
        assert doc.extracted_text != "late text"


def test_claim_token_guards_every_commit_and_allows_the_final_status():
    with SessionLocal() as db:
        doc = _doc(db, _kb(db)); _job(db, doc)
        job = claim_next_job(db)
        assert job.claim_token
        with own_job(db, job):
            job.status = "completed"
            db.flush()
            db.commit()  # finishing its own job is allowed
            db.query(ProcessingJob).filter_by(id=job.id).update({"claim_token": "someone-else"}, synchronize_session=False)
            with pytest.raises(ClaimLost):
                db.commit()
        db.rollback()
        db.commit()  # outside own_job nothing is guarded


# ------------------------------------------------------------ Finding 6 / nits
def test_graph_events_stop_after_repeated_worker_losses():
    with SessionLocal() as db:
        worn = GraphProjectionEvent(event_type="entity", status="running", attempt_count=GRAPH_EVENT_MAX_ATTEMPTS)
        db.add(worn); db.commit()
        db.query(GraphProjectionEvent).filter_by(id=worn.id).update({"updated_at": datetime.utcnow() - timedelta(hours=2)}, synchronize_session=False)
        db.commit()
        assert sweep_stale_jobs(db) >= 1
        db.expire_all()
        assert db.get(GraphProjectionEvent, worn.id).status == "failed"


# ------------------------------------------------------------ Finding 7
def test_worker_pass_survives_a_failing_sweep(monkeypatch):
    def broken(_db):
        raise RuntimeError("database went away")

    monkeypatch.setattr(worker, "sweep_stale_jobs", broken)
    monkeypatch.setattr(worker, "process_next_graph_projection", lambda _db: False)
    now = time.monotonic()  # retention and recovery just ran; only the sweep is due
    state = {"retention": now, "recovery": now, "stale_sweep": 0.0, "provider_probe": now}
    assert worker.run_once(state) is False
    assert state["stale_sweep"] > 0


def test_failed_job_rolls_back_a_broken_session(monkeypatch):
    """A flush error poisons the session; the failure must still be recorded."""
    with SessionLocal() as db:
        doc = _doc(db, _kb(db)); job = _job(db, doc)
        monkeypatch.setattr(services, "extract_text", lambda _doc: "body")

        def broken_flush(session, _doc_id):
            session.add(ProcessingJob(id=job.id, job_type=JobType.PROCESS_DOCUMENT))  # duplicate primary key
            session.flush()

        monkeypatch.setattr(services, "embed_document_chunks", broken_flush)
        assert services.process_next_job(db) is True
        db.expire_all()
        assert db.get(ProcessingJob, job.id).status == "failed"
