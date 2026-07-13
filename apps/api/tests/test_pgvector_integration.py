"""Optional real pgvector coverage, run by docker-compose.pgvector-test.yml."""
import os
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Document, DocumentChunk, KnowledgeBase
from app.openrouter import OpenRouterClient
from app.services import query_database_vectors


TEST_DATABASE_URL = os.getenv("TEST_POSTGRES_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="requires TEST_POSTGRES_DATABASE_URL")


def test_pgvector_retrieval_uses_a_real_vector_index(monkeypatch):
    engine = create_engine(TEST_DATABASE_URL)
    vector = [1.0] + [0.0] * 1535
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
            monkeypatch.setattr(OpenRouterClient, "embeddings_enabled", property(lambda _: True))
            monkeypatch.setattr(OpenRouterClient, "embed_texts", lambda _self, _texts: [vector])
            trace = []
            result = query_database_vectors(db, "semantic evidence", [kb.id], 5, trace)
            assert len(result.sources) == 1
            assert result.sources[0]["title"] == "Vector evidence"
            assert trace[-1]["channel"] == "semantic_vector" and trace[-1]["status"] == "used"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
