"""Real PostgreSQL coverage for concurrent job claims, lock order and claim tokens.

Run with TEST_POSTGRES_DATABASE_URL naming a throwaway *_test database (see
postgres_safety.py or ``make test-postgres``)."""
import threading
import time
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db import Base
from postgres_safety import postgres_test_url
from app import metadata_extraction
from app.job_queue import ClaimLost, claim_next_job, disown, job_heartbeat, lock_knowledge_base_graph, own_job, sweep_stale_jobs
from app.models import Document, JobType, KnowledgeBase, ProcessingJob

TEST_DATABASE_URL = postgres_test_url()  # refuses any database not named *_test
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="requires TEST_POSTGRES_DATABASE_URL")


@pytest.fixture()
def Session():
    engine = create_engine(TEST_DATABASE_URL, pool_size=20)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed(Session, documents=10, jobs_per_document=3):
    with Session() as db:
        kb = KnowledgeBase(name="pg queue", code="pg-queue", status="active", retrieval_config={})
        db.add(kb); db.flush()
        base = datetime.utcnow() - timedelta(minutes=10)
        for index in range(documents):
            doc = Document(knowledge_base_id=kb.id, original_filename=f"{index}.txt", stored_filename=f"{index}.txt",
                           storage_path=f"/tmp/{index}.txt", mime_type="text/plain", file_size=1,
                           checksum_sha256=uuid.uuid4().hex * 2, title=str(index), document_type="general", tags=[], status="queued")
            db.add(doc); db.flush()
            for step, job_type in enumerate([JobType.PROCESS_DOCUMENT, JobType.EXTRACT_DOCUMENT_METADATA, JobType.EXTRACT_LEGAL_METADATA][:jobs_per_document]):
                db.add(ProcessingJob(document_id=doc.id, knowledge_base_id=kb.id, job_type=job_type,
                                     created_at=base + timedelta(seconds=index * 10 + step)))
        db.commit()
        return kb.id


def test_parallel_workers_claim_each_job_once_and_never_share_a_document(Session):
    _seed(Session)
    claims: list[str] = []
    running_documents: set[str] = set()
    overlaps: list[str] = []
    guard = threading.Lock()

    def worker():
        with Session() as db:
            idle = 0
            while idle < 20:
                job = claim_next_job(db)
                if job is None:
                    idle += 1
                    time.sleep(0.02)
                    continue
                idle = 0
                with guard:
                    claims.append(job.id)
                    if job.document_id in running_documents:
                        overlaps.append(job.document_id)
                    running_documents.add(job.document_id)
                time.sleep(0.03)  # "work"
                with guard:
                    running_documents.discard(job.document_id)
                job.status = "completed"
                db.commit()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    elapsed = time.monotonic() - started

    assert len(claims) == 30 and len(set(claims)) == 30, "every job claimed exactly once"
    assert overlaps == [], "two workers ran jobs of one document at the same time"
    with Session() as db:
        assert db.query(ProcessingJob).filter(ProcessingJob.status != "completed").count() == 0
    # Serial would be 30 * 0.03s = 0.9s of work; parallel workers must overlap.
    assert elapsed < 30


def test_heartbeat_keeps_a_long_job_fresh_and_sweep_releases_a_dead_one(Session, monkeypatch):
    _seed(Session, documents=2, jobs_per_document=1)
    with Session() as db:
        alive = claim_next_job(db)
        dead = claim_next_job(db)
        old = datetime.utcnow() - timedelta(hours=1)
        db.query(ProcessingJob).filter(ProcessingJob.id.in_([alive.id, dead.id])).update({"updated_at": old}, synchronize_session=False)
        db.commit()
        with job_heartbeat(db, alive.id, interval=0.1):
            time.sleep(0.5)
        db.expire_all()
        assert db.get(ProcessingJob, alive.id).updated_at > old + timedelta(minutes=30)
        assert sweep_stale_jobs(db) == 1
        db.expire_all()
        assert db.get(ProcessingJob, dead.id).status == "queued"
        assert db.get(ProcessingJob, alive.id).status == "running"


def test_knowledge_base_graph_lock_serializes_writers(Session):
    kb_id = _seed(Session, documents=1, jobs_per_document=1)
    order: list[str] = []

    def writer(name, hold):
        with Session() as db:
            lock_knowledge_base_graph(db, kb_id)
            order.append(f"{name}:in")
            time.sleep(hold)
            order.append(f"{name}:out")
            db.commit()

    first = threading.Thread(target=writer, args=("a", 0.4))
    first.start(); time.sleep(0.1)
    second = threading.Thread(target=writer, args=("b", 0))
    second.start(); first.join(); second.join()
    assert order == ["a:in", "a:out", "b:in", "b:out"]


def test_sweep_runs_in_one_process_at_a_time(Session):
    with Session() as holder, Session() as other:
        holder.execute(text("SELECT pg_advisory_xact_lock(7413, hashtext('stale-job-sweep'))"))
        assert sweep_stale_jobs(other) == 0
        holder.rollback()


def test_metadata_job_and_rebuild_take_locks_in_the_same_order(Session, monkeypatch):
    """Review finding 1: KB graph lock before the document row, in every writer.

    The "rebuild" thread holds the KB lock and then locks the document row.  A
    metadata job that locked the document row first and then waited for the KB
    lock would deadlock with it.
    """
    kb_id = _seed(Session, documents=1, jobs_per_document=0)
    with Session() as db:
        doc = db.query(Document).one()
        doc.extracted_text = "metadata source"
        db.add(ProcessingJob(document_id=doc.id, knowledge_base_id=kb_id, job_type=JobType.EXTRACT_DOCUMENT_METADATA))
        db.commit()
        doc_id = doc.id
    monkeypatch.setattr(metadata_extraction, "extract_candidates", lambda *_a, **_k: {})
    errors: list[BaseException] = []
    kb_held, metadata_waiting = threading.Event(), threading.Event()

    def rebuild():
        try:
            with Session() as db:
                lock_knowledge_base_graph(db, kb_id)
                kb_held.set()
                metadata_waiting.wait(5)
                time.sleep(0.3)  # the metadata job is now blocked on the KB lock
                db.execute(text("SET LOCAL lock_timeout = '5s'"))
                db.query(Document).filter_by(id=doc_id).with_for_update().one()
                db.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def metadata():
        try:
            with Session() as db:
                job = claim_next_job(db)
                assert job is not None and job.job_type == JobType.EXTRACT_DOCUMENT_METADATA
                kb_held.wait(5)
                metadata_waiting.set()
                db.execute(text("SET lock_timeout = '5s'"))
                metadata_extraction.process_metadata_job(db, job)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=rebuild), threading.Thread(target=metadata)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    with Session() as db:
        assert db.query(ProcessingJob).one().status == "completed"


def test_document_row_is_free_while_the_metadata_job_waits_for_the_kb_lock(Session, monkeypatch):
    kb_id = _seed(Session, documents=1, jobs_per_document=0)
    with Session() as db:
        doc = db.query(Document).one()
        doc.extracted_text = "metadata source"
        db.add(ProcessingJob(document_id=doc.id, knowledge_base_id=kb_id, job_type=JobType.EXTRACT_DOCUMENT_METADATA))
        db.commit()
        doc_id = doc.id
    monkeypatch.setattr(metadata_extraction, "extract_candidates", lambda *_a, **_k: {})
    with Session() as holder:
        lock_knowledge_base_graph(holder, kb_id)
        worker = Session()
        job = claim_next_job(worker)
        thread = threading.Thread(target=metadata_extraction.process_metadata_job, args=(worker, job))
        thread.start()
        time.sleep(0.5)
        with Session() as probe:
            probe.execute(text("SET LOCAL lock_timeout = '1s'"))
            probe.query(Document).filter_by(id=doc_id).with_for_update(nowait=True).one()  # not held by the waiting job
            probe.rollback()
        holder.commit()
        thread.join(timeout=10)
        worker.close()


def test_claim_token_rejects_a_superseded_worker_and_its_heartbeat(Session):
    _seed(Session, documents=1, jobs_per_document=1)
    with Session() as db, Session() as api:
        job = claim_next_job(db)
        token = job.claim_token
        old = datetime.utcnow() - timedelta(hours=1)
        with own_job(db, job):
            # A reprocess / stale sweep hands the job over.
            stolen = api.get(ProcessingJob, job.id)
            disown(stolen)
            stolen.status = "failed"
            api.commit()
            api.query(ProcessingJob).filter_by(id=job.id).update({"updated_at": old}, synchronize_session=False)
            api.commit()
            job.status = "completed"
            with pytest.raises(ClaimLost):
                db.commit()
            db.rollback()
        # The old worker's heartbeat does not keep the replaced job alive.
        with job_heartbeat(db, job.id, interval=0.05, token=token):
            time.sleep(0.3)
        api.expire_all()
        row = api.get(ProcessingJob, job.id)
        assert row.status == "failed" and row.claim_token is None and row.updated_at < old + timedelta(minutes=1)
