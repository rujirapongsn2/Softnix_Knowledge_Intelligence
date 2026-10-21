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
        assert provider_holds.held_job_types(db) == [] and provider_holds.hold_view(db) == []
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
    """LightRAG still processing this document from the attempt that stopped on 402, as engine.ingest sees it."""
    def __init__(self, statuses, finished_track="track-existing"):
        self.statuses, self.finished_track, self.ingests = list(statuses), finished_track, 0

    def pipeline_progress(self):
        return None

    def find_document(self, document_id, knowledge_base_id):
        # Each look at the record lets LightRAG move on to its next status.
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return None if status is None else {"id": "doc-remote", "status": status}

    def ingest(self, document_id, knowledge_base_id, text, title):
        self.ingests += 1
        if self.statuses[0] in ("pending", "processing"):
            raise services.RetrievalEngineError("RETRIEVAL_ENGINE_BUSY", "HTTP 409: Document storage already contains it")
        # engine.ingest reuses a processed record's track and replaces a failed one.
        return self.finished_track


def test_a_resumed_document_waits_for_its_stopped_attempt_and_keeps_the_track(monkeypatch):
    monkeypatch.setattr(services.time, "sleep", lambda _seconds: None)
    engine = _LeftoverEngine(["processing", "processing", "processed"])
    with SessionLocal() as db:
        assert services._ingest_after_own_attempt(engine, _doc(db), "body", "t") == "track-existing"
    assert engine.ingests == 3


class _BusyDeleteEngine(_LeftoverEngine):
    """The stopped attempt left a failed record, and LightRAG refuses to delete it while other documents index."""
    def __init__(self, busy_deletes):
        super().__init__(["failed"])
        self.busy_deletes = busy_deletes

    def ingest(self, document_id, knowledge_base_id, text, title):
        self.ingests += 1
        if self.busy_deletes:
            self.busy_deletes -= 1
            raise services.RetrievalEngineError("RETRIEVAL_ENGINE_BUSY", "LightRAG refused the delete while its pipeline is busy.")
        return self.finished_track


def test_a_failed_record_that_lightrag_will_not_delete_yet_is_waited_for(monkeypatch):
    monkeypatch.setattr(services.time, "sleep", lambda _seconds: None)
    engine = _BusyDeleteEngine(busy_deletes=4)
    with SessionLocal() as db:
        assert services._ingest_after_own_attempt(engine, _doc(db), "body", "t") == "track-existing"
    assert engine.ingests == 5, "asked again after each busy refusal instead of failing the document"


def test_a_busy_engine_without_a_record_of_this_document_is_still_busy():
    with SessionLocal() as db, pytest.raises(services.RetrievalEngineError, match="RETRIEVAL_ENGINE_BUSY"):
        engine = _LeftoverEngine(["processing"])
        engine.find_document = lambda *_args: None
        services._ingest_after_own_attempt(engine, _doc(db), "body", "t")


def test_waiting_for_a_stuck_attempt_ends_in_a_timeout(monkeypatch):
    clock = iter(range(0, 10_000, 500))
    monkeypatch.setattr(services.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(services.time, "sleep", lambda _seconds: None)
    with SessionLocal() as db, pytest.raises(services.RetrievalEngineError, match="RETRIEVAL_ENGINE_TIMEOUT"):
        services._ingest_after_own_attempt(_LeftoverEngine(["processing"]), _doc(db), "body", "t")


def test_a_document_waits_past_the_timeout_while_the_pipeline_keeps_moving(monkeypatch):
    """A long manual indexes for longer than LIGHTRAG_PROCESSING_TIMEOUT_SECONDS; only a stalled pipeline times out."""
    clock = {"now": 0}
    monkeypatch.setattr(services.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(services.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + 300))
    stall = services.get_settings().lightrag_processing_timeout_seconds
    engine = _LeftoverEngine(["processing"] * 40 + ["processed"])
    chunks = iter(range(1_000))
    engine.pipeline_progress = lambda: ("job", 1, f"Chunk {next(chunks)} extracted", 0)
    with SessionLocal() as db:
        assert services._ingest_after_own_attempt(engine, _doc(db), "body", "t") == "track-existing"
    assert clock["now"] > 3 * stall, "waited well past the timeout because every look showed progress"

    clock["now"] = 0
    engine = _LeftoverEngine(["processing"])
    engine.pipeline_progress = lambda: ("job", 1, "Chunk 7 extracted", 0)
    with SessionLocal() as db, pytest.raises(services.RetrievalEngineError, match="RETRIEVAL_ENGINE_TIMEOUT"):
        services._ingest_after_own_attempt(engine, _doc(db), "body", "t")
    assert stall <= clock["now"] < stall + 600, "a pipeline that stands still times out after the timeout"


def test_a_job_that_keeps_failing_alone_stops_waiting_and_fails(monkeypatch):
    _fail_embedding_with(monkeypatch, "LLM_CREDITS_EXHAUSTED")
    with SessionLocal() as db:
        job = _job(db, _doc(db))
        job.deferral_count = provider_holds.MAX_PROVIDER_WAITS
        db.commit()
        services.process_next_job(db)
        db.expire_all()
        job = db.get(ProcessingJob, job.id)
        assert (job.status, job.error_code) == ("failed", "LLM_CREDITS_EXHAUSTED")
        assert db.query(ProviderHold).count() == 0, "a job past its waits does not hold the queue again"


def test_a_late_failure_right_after_a_lift_waits_alone_without_holding_the_queue(monkeypatch):
    _fail_embedding_with(monkeypatch, "LLM_CREDITS_EXHAUSTED")
    with SessionLocal() as db:
        provider_holds.place_hold(db, "llm", "LLM_CREDITS_EXHAUSTED"); db.commit()
        monkeypatch.setitem(provider_holds.PROBES, "llm", lambda: None)
        assert provider_holds.probe_holds(db, now=datetime.utcnow() + timedelta(minutes=2)) == ["llm"]
        late = _job(db, _doc(db))
        services.process_next_job(db)
        other = _job(db, _doc(db), JobType.EXTRACT_DOCUMENT_METADATA)
        db.expire_all()
        assert db.get(ProcessingJob, late.id).current_stage == "waiting_for_provider"
        assert provider_holds.held_job_types(db) == []
        assert claim_next_job(db).id == other.id
        # After the grace period the same failure holds the queue again.
        provider_holds.place_hold(db, "llm", "LLM_CREDITS_EXHAUSTED",
                                  now=datetime.utcnow() + timedelta(seconds=provider_holds.LIFT_GRACE_SECONDS + 1))
        db.commit()
        assert JobType.PROCESS_DOCUMENT in provider_holds.held_job_types(db)


def test_the_llm_probe_also_asks_the_model_lightrag_extracts_with(monkeypatch):
    asked = []

    class Client:
        def probe(self, model=None):
            asked.append(model)
            return "LLM_CREDITS_EXHAUSTED" if model == "openai/gpt-4o-mini" else None
    monkeypatch.setattr(provider_holds, "LLMClient", Client)
    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-sonnet-5.5")
    monkeypatch.setenv("LIGHTRAG_LLM_MODEL", "openai/gpt-4o-mini")
    provider_holds.get_settings.cache_clear()
    assert provider_holds._probe_llm() == "LLM_CREDITS_EXHAUSTED"
    assert asked == ["anthropic/claude-sonnet-5.5", "openai/gpt-4o-mini"]
    provider_holds.get_settings.cache_clear()
