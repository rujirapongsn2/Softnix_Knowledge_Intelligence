"""Optional real pgvector coverage, run by docker-compose.pgvector-test.yml."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import Base
from app.embeddings import EmbeddingClient
from postgres_safety import postgres_test_url
from app.models import Document, DocumentChunk, KnowledgeBase
from app.services import query_database_vectors


TEST_DATABASE_URL = postgres_test_url()  # refuses any database not named *_test
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="requires TEST_POSTGRES_DATABASE_URL")


def test_pgvector_retrieval_uses_a_real_vector_index(monkeypatch):
    engine = create_engine(TEST_DATABASE_URL)
    vector = [1.0] + [0.0] * (get_settings().embedding_dimension - 1)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session() as db:
            kb = KnowledgeBase(code="pgvector-test", name="pgvector test", status="active", retrieval_config={})
            db.add(kb); db.flush()
            document = Document(knowledge_base_id=kb.id, original_filename="vector.txt", stored_filename="vector.txt",
                                storage_path="/tmp/vector.txt", mime_type="text/plain", file_size=20, checksum_sha256="1" * 64,
                                title="Vector evidence", document_type="general", tags=[], status="completed",
                                extracted_text="Semantic vector evidence", indexed_at=datetime.utcnow())
            db.add(document); db.flush()
            db.add(DocumentChunk(document_id=document.id, knowledge_base_id=kb.id, chunk_index=0,
                                 content="Semantic vector evidence", content_sha256="2" * 64, char_start=0, char_end=24,
                                 token_count=3, embedding=vector))
            db.commit()
            monkeypatch.setattr(EmbeddingClient, "enabled", property(lambda _: True))
            monkeypatch.setattr(EmbeddingClient, "embed_texts", lambda _self, _texts, _purpose: [vector])
            trace = []
            result = query_database_vectors(db, "semantic evidence", [kb.id], 5, trace)
            assert len(result.sources) == 1
            assert result.sources[0]["title"] == "Vector evidence"
            assert trace[-1]["channel"] == "semantic_vector" and trace[-1]["status"] == "used"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_a_small_knowledge_base_gets_vector_results_when_a_larger_one_holds_the_nearest_chunks(monkeypatch):
    engine = create_engine(TEST_DATABASE_URL)
    dimension = get_settings().embedding_dimension

    def unit(index: int, weight: float) -> list[float]:
        vector = [0.0] * dimension
        vector[0], vector[index] = 1.0, weight
        return vector

    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE INDEX ix_document_chunks_embedding_hnsw ON document_chunks USING hnsw (embedding vector_cosine_ops)")
            # On dev's 79,000 chunks PostgreSQL picks the HNSW index for a Knowledge Base filter; on a few hundred rows
            # it would read the btree indexes instead, so they are dropped to reach the same plan.
            for (name,) in connection.exec_driver_sql("SELECT conname FROM pg_constraint WHERE conrelid = 'document_chunks'::regclass AND contype = 'u'").all():
                connection.exec_driver_sql(f'ALTER TABLE document_chunks DROP CONSTRAINT "{name}"')
            for (name,) in connection.exec_driver_sql("SELECT indexname FROM pg_indexes WHERE tablename = 'document_chunks' "
                                                      "AND indexname NOT IN ('ix_document_chunks_embedding_hnsw', 'document_chunks_pkey')").all():
                connection.exec_driver_sql(f'DROP INDEX "{name}"')
        Session = sessionmaker(bind=engine)
        with Session() as db:
            kbs = {}
            for code, count, weight in (("large", 400, 0.01), ("small", 3, 1.0)):
                kb = KnowledgeBase(code=f"pgvector-{code}", name=code, status="active", retrieval_config={})
                db.add(kb); db.flush()
                document = Document(knowledge_base_id=kb.id, original_filename=f"{code}.txt", stored_filename=f"{code}.txt",
                                    storage_path=f"/tmp/{code}.txt", mime_type="text/plain", file_size=20, checksum_sha256=code[0] * 64,
                                    title=code, document_type="general", tags=[], status="completed", extracted_text=code,
                                    indexed_at=datetime.utcnow())
                db.add(document); db.flush()
                db.add_all(DocumentChunk(document_id=document.id, knowledge_base_id=kb.id, chunk_index=i, content=f"{code} {i}",
                                         content_sha256=f"{i:064d}", char_start=0, char_end=1, token_count=1,
                                         embedding=unit(1 + i % (dimension - 1), weight)) for i in range(count))
                kbs[code] = kb.id
            db.commit()
            monkeypatch.setattr(EmbeddingClient, "enabled", property(lambda _: True))
            monkeypatch.setattr(EmbeddingClient, "embed_texts", lambda _self, _texts, _purpose: [unit(0, 0.0)])
            # The large Knowledge Base holds every chunk nearest the query; the index must still serve the small one.
            db.execute(text("SET enable_seqscan = off"))
            result = query_database_vectors(db, "question", [kbs["small"]], 5)
            assert [source["title"] for source in result.sources] == ["small"] * 3
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
