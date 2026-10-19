"""Queue claiming, provider retry schedule, and stale-job recovery (SQLite path).

The multi-process ``FOR UPDATE SKIP LOCKED`` behaviour is covered against a
real PostgreSQL in test_job_queue_postgres.py.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import services
from app.db import Base, SessionLocal, engine
from app.job_queue import STALE_MAX_LOSSES, WORKER_LOST, claim_next_job, is_stale, sweep_stale_jobs
from app.main import app
from app.models import Document, GraphProjectionEvent, JobType, KnowledgeBase, ProcessingJob
from app.retry_policy import PROCESSING_RETRY_POLICIES, PROVIDER_RETRY, schedule_retry


@pytest.fixture(autouse=True)
def _schema():
    # Tables normally appear with the app's startup; these tests also run without a client.
    Base.metadata.create_all(engine)


def _kb(db):
    kb = KnowledgeBase(name="Queue KB", code=f"queue-{uuid.uuid4().hex[:10]}", status="active")
    db.add(kb); db.commit()
    return kb


def _doc(db, kb, status="queued", deleted=False):
    doc = Document(knowledge_base_id=kb.id, original_filename="q.txt", stored_filename=f"{uuid.uuid4()}.txt",
                   storage_path="/tmp/does-not-matter.txt", mime_type="text/plain", file_size=1,
                   checksum_sha256=uuid.uuid4().hex * 2, title="q", tags=[], status=status,
                   deleted_at=datetime.utcnow() if deleted else None)
    db.add(doc); db.commit()
    return doc


def _isolate_queue(db):
    """Other test modules share this database: park their queued jobs so claims only see ours."""
    db.query(ProcessingJob).filter(ProcessingJob.status.in_(["queued", "running"])).update({"status": "cancelled"}, synchronize_session=False)
    db.commit()


def test_claim_respects_follow_up_priority_and_document_exclusivity():
    with SessionLocal() as db:
        _isolate_queue(db)
        kb = _kb(db)
        busy, free = _doc(db, kb), _doc(db, kb)
        now = datetime.utcnow()
        running = ProcessingJob(document_id=busy.id, knowledge_base_id=kb.id, job_type=JobType.PROCESS_DOCUMENT, status="running")
        blocked_follow_up = ProcessingJob(document_id=busy.id, knowledge_base_id=kb.id, job_type=JobType.EXTRACT_LEGAL_METADATA,
                                          created_at=now - timedelta(minutes=5))
        purge = ProcessingJob(document_id=free.id, knowledge_base_id=kb.id, job_type=JobType.PURGE_REMOTE_INDEX, created_at=now - timedelta(minutes=4))
        process = ProcessingJob(document_id=free.id, knowledge_base_id=kb.id, job_type=JobType.PROCESS_DOCUMENT, created_at=now - timedelta(minutes=3))
        db.add_all([running, blocked_follow_up, purge, process]); db.commit()

        # The follow-up is oldest and highest priority, but its document already has a running job.
        claimed = claim_next_job(db)
        assert claimed.id == process.id and claimed.status == "running"
        # The purge belongs to a document that now has a running job too: nothing else is runnable.
        assert claim_next_job(db) is None
        assert db.get(ProcessingJob, blocked_follow_up.id).status == "queued"

        running.status = "completed"; db.commit()
        assert claim_next_job(db).id == blocked_follow_up.id


def test_a_claimed_job_leaves_its_queued_stage_behind():
    with SessionLocal() as db:
        _isolate_queue(db)
        doc = _doc(db, _kb(db))
        job = ProcessingJob(document_id=doc.id, knowledge_base_id=doc.knowledge_base_id, job_type=JobType.PROCESS_DOCUMENT,
                            current_stage="retry_wait", error_code=WORKER_LOST)
        db.add(job); db.commit()
        claimed = claim_next_job(db)
        assert claimed.id == job.id and claimed.status == "running"
        # A running job must not show the stage it waited in; the runner's first stage follows.
        assert claimed.current_stage == "claimed"


def test_claim_skips_jobs_not_yet_due():
    with SessionLocal() as db:
        _isolate_queue(db)
        kb = _kb(db)
        doc = _doc(db, kb)
        later = ProcessingJob(document_id=doc.id, knowledge_base_id=kb.id, job_type=JobType.PROCESS_DOCUMENT,
                              next_attempt_at=datetime.utcnow() + timedelta(minutes=5))
        db.add(later); db.commit()
        assert claim_next_job(db) is None


def test_provider_failures_use_long_non_blocking_retry_schedule():
    policy = PROCESSING_RETRY_POLICIES["OPENROUTER_UNAVAILABLE"]
    assert policy is PROVIDER_RETRY and policy.max_attempts == 6
    assert [policy.delay(attempt) for attempt in range(1, 6)] == [60, 120, 240, 480, 900]

    class Job:
        attempt_count = 1
        next_attempt_at = None

    job = Job()
    before = datetime.utcnow()
    assert schedule_retry(job, policy) is True
    assert job.next_attempt_at >= before + timedelta(seconds=60)
    job.attempt_count = 6
    assert schedule_retry(job, policy) is False


def test_openrouter_outage_requeues_document_job_for_later(monkeypatch):
    with SessionLocal() as db:
        _isolate_queue(db)
        kb = _kb(db)
        doc = _doc(db, kb)
        job = ProcessingJob(document_id=doc.id, knowledge_base_id=kb.id, job_type=JobType.PROCESS_DOCUMENT)
        db.add(job); db.commit()
        monkeypatch.setattr(services, "extract_text", lambda _doc: "body text")

        def outage(_db, _document_id):
            raise RuntimeError("OPENROUTER_UNAVAILABLE")

        monkeypatch.setattr(services, "embed_document_chunks", outage)
        started = datetime.utcnow()
        assert services.process_next_job(db) is True
        db.expire_all()
        job, doc = db.get(ProcessingJob, job.id), db.get(Document, doc.id)
        assert job.status == "queued" and job.current_stage == "retry_wait" and job.error_code == "OPENROUTER_UNAVAILABLE"
        assert job.next_attempt_at >= started + timedelta(seconds=60)
        assert doc.status == "queued"
        # Not due yet, so the worker moves on instead of hammering the provider.
        assert claim_next_job(db) is None


def test_stale_sweep_requeues_lost_jobs_and_leaves_live_ones():
    with SessionLocal() as db:
        _isolate_queue(db)
        kb = _kb(db)
        lost_doc, live_doc, gone_doc, worn_doc = _doc(db, kb, "indexing"), _doc(db, kb, "indexing"), _doc(db, kb, deleted=True), _doc(db, kb, "extracting")
        old = datetime.utcnow() - timedelta(hours=2)
        lost = ProcessingJob(document_id=lost_doc.id, knowledge_base_id=kb.id, job_type=JobType.PROCESS_DOCUMENT, status="running", attempt_count=1)
        live = ProcessingJob(document_id=live_doc.id, knowledge_base_id=kb.id, job_type=JobType.PROCESS_DOCUMENT, status="running", attempt_count=1)
        gone = ProcessingJob(document_id=gone_doc.id, knowledge_base_id=kb.id, job_type=JobType.PROCESS_DOCUMENT, status="running", attempt_count=1)
        # Provider retries (attempt_count) do not count towards the lost-worker cap (lost_count).
        lost.attempt_count = 5
        worn = ProcessingJob(document_id=worn_doc.id, knowledge_base_id=kb.id, job_type=JobType.REPROCESS_DOCUMENT, status="running",
                             attempt_count=1, lost_count=STALE_MAX_LOSSES - 1)
        event = GraphProjectionEvent(event_type="entity", status="running")
        db.add_all([lost, live, gone, worn, event]); db.commit()
        for row in (lost, gone, worn):
            db.query(ProcessingJob).filter_by(id=row.id).update({"updated_at": old}, synchronize_session=False)
        db.query(GraphProjectionEvent).filter_by(id=event.id).update({"updated_at": old}, synchronize_session=False)
        db.commit()

        assert sweep_stale_jobs(db) == 4
        db.expire_all()
        assert db.get(ProcessingJob, lost.id).status == "queued"
        assert db.get(ProcessingJob, lost.id).error_code == WORKER_LOST
        assert db.get(Document, lost_doc.id).status == "queued"
        assert db.get(ProcessingJob, live.id).status == "running"
        assert db.get(ProcessingJob, gone.id).status == "cancelled"
        assert db.get(ProcessingJob, worn.id).status == "failed"
        assert db.get(Document, worn_doc.id).status == "failed" and db.get(Document, worn_doc.id).error_code == WORKER_LOST
        assert db.get(GraphProjectionEvent, event.id).status == "queued"
        # The re-queued job is claimable right away.
        assert claim_next_job(db).id == lost.id


def test_reprocess_hands_a_stale_job_over_the_way_the_sweep_does():
    """A reprocess must not disagree with the sweep: the loss is counted. The superseded metadata job leaves no
    "queued" status without a job behind; the document's review state comes from the values it holds."""
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        with SessionLocal() as db:
            kb = _kb(db)
            metadata_doc, worn_doc = _doc(db, kb, "completed"), _doc(db, kb, "indexing")
            metadata_doc.metadata_status = "running"
            metadata_doc.metadata_template_fields = [{"key": "issuer", "label": "Issuer", "field_type": "text", "fill_mode": "extract"}]
            metadata_doc.document_metadata = {"issuer": "Land Dept"}
            metadata_doc.metadata_observations = {"issuer": {"status": "suggested", "origin": "document_extraction",
                                                             "candidates": [{"value": "Land Dept", "evidence": {"quote": "Land Dept"}}]}}
            metadata = ProcessingJob(document_id=metadata_doc.id, knowledge_base_id=kb.id, job_type=JobType.EXTRACT_DOCUMENT_METADATA,
                                     status="running", claim_token="dead-worker")
            worn = ProcessingJob(document_id=worn_doc.id, knowledge_base_id=kb.id, job_type=JobType.PROCESS_DOCUMENT,
                                 status="running", claim_token="dead-worker", lost_count=STALE_MAX_LOSSES - 1)
            db.add_all([metadata, worn]); db.commit()
            db.query(ProcessingJob).filter(ProcessingJob.id.in_([metadata.id, worn.id])).update({"updated_at": datetime.utcnow() - timedelta(hours=1)}, synchronize_session=False)
            db.commit()
            ids = metadata_doc.id, worn_doc.id, metadata.id, worn.id

        assert client.post(f"/api/v1/documents/{ids[0]}/reprocess").status_code == 200
        assert client.post(f"/api/v1/documents/{ids[1]}/reprocess").status_code == 200
        with SessionLocal() as db:
            old = db.get(ProcessingJob, ids[2])
            assert (old.status, old.error_code, old.lost_count, old.claim_token) == ("failed", WORKER_LOST, 1, None)
            assert old.error_message == "Superseded by a reprocess after the worker stopped."
            assert db.get(Document, ids[0]).metadata_status == "unverified"
            assert not db.query(ProcessingJob).filter_by(document_id=ids[0], job_type=JobType.EXTRACT_DOCUMENT_METADATA, status="queued").count()
            worn = db.get(ProcessingJob, ids[3])
            assert (worn.status, worn.lost_count) == ("failed", STALE_MAX_LOSSES)
            assert db.get(Document, ids[1]).status == "queued", "the reprocess itself is queued"


def test_reprocess_replaces_a_stale_running_job_but_not_a_live_one():
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        with SessionLocal() as db:
            kb = _kb(db)
            stale_doc, live_doc = _doc(db, kb, "indexing"), _doc(db, kb, "indexing")
            stale = ProcessingJob(document_id=stale_doc.id, knowledge_base_id=kb.id, job_type=JobType.PROCESS_DOCUMENT, status="running")
            live = ProcessingJob(document_id=live_doc.id, knowledge_base_id=kb.id, job_type=JobType.PROCESS_DOCUMENT, status="running")
            db.add_all([stale, live]); db.commit()
            db.query(ProcessingJob).filter_by(id=stale.id).update({"updated_at": datetime.utcnow() - timedelta(hours=1)}, synchronize_session=False)
            db.commit()
            db.expire_all()
            assert is_stale(db.get(ProcessingJob, stale.id)) and not is_stale(db.get(ProcessingJob, live.id))
            stale_id, stale_doc_id, live_doc_id = stale.id, stale_doc.id, live_doc.id

        response = client.post(f"/api/v1/documents/{stale_doc_id}/reprocess")
        assert response.status_code == 200 and response.json()["status"] == "queued"
        assert client.post(f"/api/v1/documents/{live_doc_id}/reprocess").status_code == 409
        with SessionLocal() as db:
            old = db.get(ProcessingJob, stale_id)
            assert old.status == "failed" and old.error_code == WORKER_LOST
