"""Item 4 tests: delete cascade (PURGE_REMOTE_INDEX) + F8 partial unique."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Document, KnowledgeBase, ProcessingJob
from app import services


SQLA_URL = "sqlite://"


@pytest.fixture()
def db():
    engine = create_engine(SQLA_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _seed_kb_doc(db, kb_code="kb-x", title="t"):
    kb = KnowledgeBase(id="kb-1", name="KB", code=kb_code)
    db.add(kb)
    db.commit()
    doc = Document(
        id="doc-1", knowledge_base_id="kb-1", original_filename="a.txt",
        stored_filename="a.txt", storage_path="/tmp/a.txt", mime_type="text/plain",
        file_size=1, checksum_sha256="c" * 64, title=title,
    )
    db.add(doc)
    db.commit()
    return kb, doc


class TestPurgeRemoteIndexJob:
    def test_purge_job_deletes_remote_source(self, db, monkeypatch):
        kb, doc = _seed_kb_doc(db)
        calls = []

        class FakeEngine:
            enabled = True

            def find_document(self, document_id, knowledge_base_id):
                calls.append(("find", document_id))
                return {"id": "doc-remote-1"}

            def delete_remote_document(self, remote_id):
                calls.append(("delete", remote_id))

        monkeypatch.setattr(services, "LightRAGRetrievalEngine", lambda: FakeEngine())
        job = ProcessingJob(document_id=doc.id, knowledge_base_id="kb-1", job_type="PURGE_REMOTE_INDEX")
        db.add(job)
        db.commit()

        assert services.process_next_job(db) is True
        job = db.query(ProcessingJob).filter_by(job_type="PURGE_REMOTE_INDEX").one()
        assert job.status == "completed"
        assert ("delete", "doc-remote-1") in calls

    def test_purge_job_no_remote_source_completes(self, db, monkeypatch):
        kb, doc = _seed_kb_doc(db)

        class FakeEngine:
            enabled = True

            def find_document(self, *a, **k):
                return None

            def delete_remote_document(self, *a, **k):
                raise AssertionError("must not delete when nothing found")

        monkeypatch.setattr(services, "LightRAGRetrievalEngine", lambda: FakeEngine())
        db.add(ProcessingJob(document_id=doc.id, knowledge_base_id="kb-1", job_type="PURGE_REMOTE_INDEX"))
        db.commit()
        assert services.process_next_job(db) is True
        job = db.query(ProcessingJob).filter_by(job_type="PURGE_REMOTE_INDEX").one()
        assert job.status == "completed"

    def test_purge_job_disabled_engine_completes(self, db, monkeypatch):
        kb, doc = _seed_kb_doc(db)

        class FakeEngine:
            enabled = False

        monkeypatch.setattr(services, "LightRAGRetrievalEngine", lambda: FakeEngine())
        db.add(ProcessingJob(document_id=doc.id, knowledge_base_id="kb-1", job_type="PURGE_REMOTE_INDEX"))
        db.commit()
        assert services.process_next_job(db) is True
        assert db.query(ProcessingJob).filter_by(job_type="PURGE_REMOTE_INDEX").one().status == "completed"

    def test_purge_job_transient_busy_schedules_retry(self, db, monkeypatch):
        """Review info fix: BUSY is transient — the job requeues, not fails."""
        kb, doc = _seed_kb_doc(db)

        class FakeEngine:
            enabled = True

            def find_document(self, *a, **k):
                return {"id": "r1"}

            def delete_remote_document(self, *a, **k):
                raise RuntimeError("RETRIEVAL_ENGINE_BUSY")

        monkeypatch.setattr(services, "LightRAGRetrievalEngine", lambda: FakeEngine())
        db.add(ProcessingJob(document_id=doc.id, knowledge_base_id="kb-1", job_type="PURGE_REMOTE_INDEX"))
        db.commit()
        assert services.process_next_job(db) is True
        job = db.query(ProcessingJob).filter_by(job_type="PURGE_REMOTE_INDEX").one()
        assert job.status == "queued"
        assert job.error_code == "RETRIEVAL_ENGINE_BUSY"
        assert job.next_attempt_at is not None

    def test_purge_job_non_transient_failure_is_terminal(self, db, monkeypatch):
        kb, doc = _seed_kb_doc(db)

        class FakeEngine:
            enabled = True

            def find_document(self, *a, **k):
                return {"id": "r1"}

            def delete_remote_document(self, *a, **k):
                raise ValueError("boom")

        monkeypatch.setattr(services, "LightRAGRetrievalEngine", lambda: FakeEngine())
        db.add(ProcessingJob(document_id=doc.id, knowledge_base_id="kb-1", job_type="PURGE_REMOTE_INDEX"))
        db.commit()
        assert services.process_next_job(db) is True
        job = db.query(ProcessingJob).filter_by(job_type="PURGE_REMOTE_INDEX").one()
        assert job.status == "failed"
        assert job.error_code == "REMOTE_INDEX_PURGE_FAILED"


class TestF8PartialUnique:
    def test_soft_deleted_doc_allows_reinsert(self, db):
        """The model-level index must permit a soft-deleted row + live twin."""
        kb, doc = _seed_kb_doc(db)
        doc.deleted_at = datetime.utcnow()
        db.commit()
        # same KB + same checksum, live row — must NOT violate uniqueness
        twin = Document(
            id="doc-2", knowledge_base_id="kb-1", original_filename="a.txt",
            stored_filename="a2.txt", storage_path="/tmp/a2.txt", mime_type="text/plain",
            file_size=1, checksum_sha256="c" * 64, title="twin",
        )
        db.add(twin)
        db.commit()  # raises IntegrityError under the OLD table constraint
        assert db.query(Document).count() == 2

    def test_live_duplicate_still_rejected(self, db):
        kb, doc = _seed_kb_doc(db)
        twin = Document(
            id="doc-2", knowledge_base_id="kb-1", original_filename="a.txt",
            stored_filename="a2.txt", storage_path="/tmp/a2.txt", mime_type="text/plain",
            file_size=1, checksum_sha256="c" * 64, title="twin",
        )
        db.add(twin)
        with pytest.raises(Exception):
            db.commit()
        db.rollback()

    def test_create_document_job_race_returns_409_semantics(self, db, monkeypatch, tmp_path):
        """F8 race guard: IntegrityError on flush surfaces FILE_DUPLICATE."""
        kb, doc = _seed_kb_doc(db)

        class FakeUpload:
            filename = "race.txt"

            def read(self):
                return b"race-content"

        import app.services as svc

        # force the pre-check to pass but the flush to violate the index
        monkeypatch.setattr(svc, "store_upload", lambda *a, **k: (str(tmp_path / "race.txt"), "race.txt", 13, "r" * 64, "text/plain"))

        def fake_first():
            return None  # pre-check sees no duplicate

        orig_query = db.query

        def query_wrapper(*entities, **kwargs):
            q = orig_query(*entities, **kwargs)
            if entities and entities[0] is Document:
                q.first = fake_first
            return q

        # simplest: patch Document query pre-check result via data — add a live
        # twin AFTER the pre-check by committing inside the hook is complex;
        # instead simulate: create twin live row with same checksum first,
        # then make the pre-check blind.
        twin = Document(
            id="doc-twin", knowledge_base_id="kb-1", original_filename="race.txt",
            stored_filename="r2.txt", storage_path="/tmp/r2.txt", mime_type="text/plain",
            file_size=13, checksum_sha256="r" * 64, title="twin",
        )
        db.add(twin)
        db.commit()

        monkeypatch.setattr(svc, "metadata_search_text", lambda *a, **k: "")

        # blind the pre-check (simulates the race window)
        real_query = db.query

        class BlindQuery:
            def __init__(self, real):
                self._real = real

            def filter_by(self, **kw):
                return self

            def filter(self, *a, **kw):
                return self

            def first(self):
                return None

        def query_fn(*entities, **kwargs):
            if entities and entities[0] is Document:
                return BlindQuery(real_query(*entities, **kwargs))
            return real_query(*entities, **kwargs)

        monkeypatch.setattr(db, "query", query_fn)

        with pytest.raises(ValueError) as exc_info:
            svc.create_document_job(db, "kb-1", FakeUpload())
        assert exc_info.value.args[0] == "FILE_DUPLICATE"
