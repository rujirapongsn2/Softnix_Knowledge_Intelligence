"""A provider out of credits (or with a rejected key) holds its jobs, and they resume on their own once it answers."""
import uuid
from datetime import datetime, timedelta

import pytest

from app import provider_holds, services
from app.db import Base, SessionLocal, engine
from app.job_queue import claim_next_job
from app.models import Document, JobType, KnowledgeBase, ProcessingJob, ProviderHold


@pytest.fixture(autouse=True)
def _clean():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.query(ProviderHold).delete()
        # Other test modules share this database: park their queued jobs so claims only see ours.
        db.query(ProcessingJob).filter(ProcessingJob.status.in_(["queued", "running"])).update({"status": "cancelled"}, synchronize_session=False)
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(ProviderHold).delete()
        db.commit()


def _doc(db, status="queued"):
    kb = KnowledgeBase(name="Hold KB", code=f"hold-{uuid.uuid4().hex[:10]}", status="active")
    db.add(kb); db.commit()
    doc = Document(knowledge_base_id=kb.id, original_filename="h.txt", stored_filename=f"{uuid.uuid4()}.txt",
                   storage_path="/tmp/does-not-matter.txt", mime_type="text/plain", file_size=1,
                   checksum_sha256=uuid.uuid4().hex * 2, title="h", tags=[], status=status, extracted_text="body text")
    db.add(doc); db.commit()
    return doc


def _job(db, doc, job_type=JobType.PROCESS_DOCUMENT):
    job = ProcessingJob(document_id=doc.id, knowledge_base_id=doc.knowledge_base_id, job_type=job_type)
    db.add(job); db.commit()
    return job


def _fail_embedding_with(monkeypatch, code):
    monkeypatch.setattr(services, "extract_text", lambda _doc: "body text")

    def fail(_db, _document_id):
        raise RuntimeError(code)
    monkeypatch.setattr(services, "embed_document_chunks", fail)


def test_running_out_of_credits_holds_the_queue_instead_of_failing_every_document(monkeypatch):
    _fail_embedding_with(monkeypatch, "LLM_CREDITS_EXHAUSTED")
    with SessionLocal() as db:
        first, second = _doc(db), _doc(db)
        first_job, second_job = _job(db, first), _job(db, second)
        assert services.process_next_job(db) is True
        db.expire_all()
        job, doc = db.get(ProcessingJob, first_job.id), db.get(Document, first.id)
        assert (job.status, job.current_stage, job.error_code, job.attempt_count) == ("queued", "waiting_for_provider", "LLM_CREDITS_EXHAUSTED", 0)
        assert (doc.status, doc.error_code) == ("queued", "LLM_CREDITS_EXHAUSTED")
        assert db.get(ProviderHold, "llm").reason_code == "LLM_CREDITS_EXHAUSTED"
        # The second document is not taken, re-extracted and failed in turn: nothing needing the LLM is claimed.
        assert claim_next_job(db) is None
        assert db.get(ProcessingJob, second_job.id).status == "queued"


def test_a_hold_leaves_jobs_that_do_not_need_the_held_provider_runnable():
    with SessionLocal() as db:
        doc = _doc(db, status="completed")
        metadata = _job(db, doc, JobType.EXTRACT_DOCUMENT_METADATA)
        reindex = _job(db, _doc(db, status="completed"), JobType.REINDEX_EMBEDDINGS)
        provider_holds.place_hold(db, "llm", "LLM_CREDITS_EXHAUSTED"); db.commit()
        claimed = claim_next_job(db)
        assert claimed.id == reindex.id
        assert db.get(ProcessingJob, metadata.id).status == "queued"


def test_topping_up_lifts_the_hold_on_the_next_due_probe_and_the_jobs_run_again(monkeypatch):
    with SessionLocal() as db:
        job = _job(db, _doc(db))
        provider_holds.place_hold(db, "llm", "LLM_CREDITS_EXHAUSTED"); db.commit()
        answers = iter(["LLM_CREDITS_EXHAUSTED", None])
        monkeypatch.setitem(provider_holds.PROBES, "llm", lambda: next(answers))

        assert provider_holds.probe_holds(db) == [], "the first probe is only due a minute after the hold"
        later = datetime.utcnow() + timedelta(seconds=provider_holds.FIRST_PROBE_SECONDS + 1)
        assert provider_holds.probe_holds(db, now=later) == []
        hold = db.get(ProviderHold, "llm")
        assert hold.probe_count == 1 and hold.last_probe_error == "LLM_CREDITS_EXHAUSTED"
        assert hold.next_probe_at >= later + timedelta(seconds=2 * provider_holds.FIRST_PROBE_SECONDS), "probes back off"
        assert claim_next_job(db) is None

        assert provider_holds.probe_holds(db, now=hold.next_probe_at) == ["llm"]
        assert db.get(ProviderHold, "llm") is None
        assert claim_next_job(db).id == job.id


def test_a_metadata_job_waits_for_credits_and_keeps_its_attempts(monkeypatch):
    from app import metadata_extraction

    with SessionLocal() as db:
        doc = _doc(db, status="completed")
        doc.metadata_template_fields = [{"key": "issuer", "label": "Issuer", "type": "text", "fill_mode": "extract"}]
        db.commit()
        job = _job(db, doc, JobType.EXTRACT_DOCUMENT_METADATA)

        def no_credits(*_args, **_kwargs):
            raise RuntimeError("LLM_CREDITS_EXHAUSTED")
        monkeypatch.setattr(metadata_extraction, "extract_candidates", no_credits)
        assert services.process_next_job(db) is True
        db.expire_all()
        job, doc = db.get(ProcessingJob, job.id), db.get(Document, doc.id)
        assert (job.status, job.current_stage, job.attempt_count) == ("queued", "waiting_for_provider", 0)
        assert doc.status == "completed" and doc.metadata_status == "queued"
        assert db.get(ProviderHold, "llm") is not None


def test_legal_extraction_waits_for_credits_instead_of_completing_without_the_llm(monkeypatch):
    with SessionLocal() as db:
        doc = _doc(db, status="completed")
        doc.document_type = "legal"
        db.commit()
        job = _job(db, doc, JobType.EXTRACT_LEGAL_METADATA)
        monkeypatch.setattr(services, "parse_legal_corpus_metadata", lambda *_a, **_k: {"instrument": {"version_role": "unknown"}})

        class NoCredits:
            def extract_legal_metadata(self, *_args):
                raise RuntimeError("LLM_CREDITS_EXHAUSTED")
        monkeypatch.setattr(services, "LLMClient", NoCredits)
        assert services.process_next_job(db) is True
        db.expire_all()
        job = db.get(ProcessingJob, job.id)
        assert (job.status, job.current_stage, job.error_code) == ("queued", "waiting_for_provider", "LLM_CREDITS_EXHAUSTED")
        assert db.get(Document, doc.id).status == "completed"


def test_an_ordinary_outage_still_uses_the_retry_schedule_and_places_no_hold(monkeypatch):
    _fail_embedding_with(monkeypatch, "LLM_UNAVAILABLE")
    with SessionLocal() as db:
        job = _job(db, _doc(db))
        services.process_next_job(db)
        db.expire_all()
        assert db.get(ProcessingJob, job.id).current_stage == "retry_wait"
        assert db.query(ProviderHold).count() == 0


class _LeftoverEngine:
    """LightRAG still holding this document from the attempt that stopped on 402."""
    def __init__(self, statuses):
        self.statuses, self.ingests, self.deleted = list(statuses), 0, []

    def ingest(self, document_id, knowledge_base_id, text, title):
        self.ingests += 1
        if self.ingests == 1:
            raise services.RetrievalEngineError("RETRIEVAL_ENGINE_BUSY", "HTTP 409 on POST /documents/text: Document storage already "
                                                "contains 'softnix-kb=k__doc=d__t' (Status: processing). Delete the existing record before re-inserting.")
        return "track-new"

    def find_document(self, document_id, knowledge_base_id):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return None if status is None else {"id": "doc-remote", "status": status}

    def delete_remote_document(self, remote_id):
        self.deleted.append(remote_id)

    def get_remote_document(self, remote_id):
        return None


@pytest.mark.parametrize("statuses, ingests, deleted, track", [
    (["processing", "processed"], 1, [], None),
    (["processing", "failed", "failed"], 2, ["doc-remote"], "track-new"),
    ([None], 2, [], "track-new"),
])
def test_a_resumed_document_settles_the_record_its_stopped_attempt_left_behind(monkeypatch, statuses, ingests, deleted, track):
    monkeypatch.setattr(services.time, "sleep", lambda _seconds: None)
    engine = _LeftoverEngine(statuses)
    with SessionLocal() as db:
        doc = _doc(db)
        assert services._ingest_over_own_leftover(engine, doc, "body", "t", deadline=float("inf")) == track
    assert (engine.ingests, engine.deleted) == (ingests, deleted)


def test_a_real_busy_engine_is_still_reported_as_busy():
    class Busy(_LeftoverEngine):
        def ingest(self, *args):
            raise services.RetrievalEngineError("RETRIEVAL_ENGINE_BUSY", "HTTP 409 on POST /documents/text: pipeline is busy")
    with SessionLocal() as db, pytest.raises(services.RetrievalEngineError, match="RETRIEVAL_ENGINE_BUSY"):
        services._ingest_over_own_leftover(Busy([None]), _doc(db), "body", "t", deadline=float("inf"))
