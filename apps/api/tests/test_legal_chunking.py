import os
import tempfile

_TEST_ROOT = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TEST_ROOT}/skip.db")
os.environ.setdefault("FILE_STORAGE_PATH", f"{_TEST_ROOT}/files")
os.environ.setdefault("INITIAL_ADMIN_PASSWORD", "correct-horse-battery-staple")
os.environ.setdefault("LIGHTRAG_BASE_URL", "")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("OPENROUTER_API_KEY", "")
os.environ.setdefault("EXT_OCR_KEY", "")

from fastapi.testclient import TestClient

from app import services
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Document, DocumentChunk, Entity, KnowledgeBase

Base.metadata.create_all(engine)


def client():
    with TestClient(app) as test_client:
        assert test_client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        yield test_client


LEGAL_TEXT = (
    "คำนำ นี่คือบทนำของกฎหมาย\n"
    "หมวด 1 บททั่วไป\n"
    "มาตรา 1 ให้ใช้กฎหมายนี้ตั้งแต่วันประกาศ\n"
    "มาตรา 2 ในกฎหมายนี้ ลูกจ้าง หมายถึง ผู้ซึ่งตกลงทำงานให้นายจ้าง\n"
    "มาตรา 15 ทวิ นายจ้างต้องจ่ายค่าชดเชยตามอัตราที่กำหนด และให้เป็นไปตามมาตรา 2 ด้วย\n"
    "ข้อ 12 รายละเอียดเพิ่มเติมของประกาศฉบับนี้\n"
)


def test_split_legal_text_assigns_section_identity_per_heading():
    pieces = services.split_legal_text(LEGAL_TEXT, chunk_size=800, overlap=50)
    labels = {piece[5] for piece in pieces if piece[5]}
    assert "มาตรา 1" in labels
    kinds = {piece[3] for piece in pieces}
    assert {"preamble", "หมวด", "มาตรา", "ข้อ"}.issubset(kinds)
    section_15 = next(piece for piece in pieces if piece[4] == "15 ทวิ")
    assert "ค่าชดเชย" in section_15[2]
    assert section_15[3] == "มาตรา"


def test_split_legal_text_does_not_treat_a_cross_reference_as_a_new_heading():
    pieces = services.split_legal_text(LEGAL_TEXT, chunk_size=800, overlap=50)
    section_15 = next(piece for piece in pieces if piece[4] == "15 ทวิ")
    # The mid-sentence "ตามมาตรา 2" reference must stay inside มาตรา 15 ทวิ's own
    # chunk instead of starting a spurious new มาตรา 2 section.
    assert "ให้เป็นไปตามมาตรา 2" in section_15[2]


def test_split_legal_text_sub_splits_a_long_section_and_keeps_its_identity():
    long_body = "มาตรา 9 " + ("เนื้อหายาวมาก " * 400)
    pieces = services.split_legal_text(long_body, chunk_size=200, overlap=20)
    assert len(pieces) > 1
    assert all(piece[3] == "มาตรา" and piece[4] == "9" for piece in pieces)


def test_split_legal_text_handles_text_with_no_headings_as_a_single_preamble():
    pieces = services.split_legal_text("Just plain prose with no legal headings at all.", 800, 50)
    assert len(pieces) == 1
    assert pieces[0][3] == "preamble"
    assert pieces[0][4] is None


def test_replace_document_chunks_uses_legal_splitter_only_for_legal_document_types():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="chunking-legal", name="Chunking legal")
        db.add(kb); db.flush()
        legal_doc = Document(knowledge_base_id=kb.id, original_filename="law.txt", stored_filename="law.txt",
                             storage_path="/tmp/law.txt", mime_type="text/plain", file_size=1, checksum_sha256="1" * 64,
                             title="Law", document_type="legal", status="completed")
        general_doc = Document(knowledge_base_id=kb.id, original_filename="memo.txt", stored_filename="memo.txt",
                               storage_path="/tmp/memo.txt", mime_type="text/plain", file_size=1, checksum_sha256="2" * 64,
                               title="Memo", document_type="general", status="completed")
        db.add_all([legal_doc, general_doc]); db.flush()
        services.replace_document_chunks(db, legal_doc, LEGAL_TEXT)
        services.replace_document_chunks(db, general_doc, LEGAL_TEXT)
        db.commit()
        legal_chunks = db.query(DocumentChunk).filter_by(document_id=legal_doc.id).all()
        general_chunks = db.query(DocumentChunk).filter_by(document_id=general_doc.id).all()
        assert any(chunk.section_kind == "มาตรา" for chunk in legal_chunks)
        assert all(chunk.section_kind is None for chunk in general_chunks)


def test_link_provisions_to_chunks_matches_by_normalized_provision_number():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="chunking-link", name="Chunking link")
        db.add(kb); db.flush()
        document = Document(knowledge_base_id=kb.id, original_filename="law.txt", stored_filename="law.txt",
                            storage_path="/tmp/law.txt", mime_type="text/plain", file_size=1, checksum_sha256="3" * 64,
                            title="Law", document_type="legal", status="completed", extracted_text=LEGAL_TEXT,
                            legal_metadata={
                                "schema_version": 2,
                                "instrument": {"kind": "act", "official_title": "Law"},
                                "provisions": [{"kind": "article", "number": "15 ทวิ", "evidence_quote": "ค่าชดเชย"}],
                                "parties": [], "obligations": [], "rights": [], "prohibitions": [], "penalties": [],
                                "definitions": [], "amendments": [], "references": [],
                            })
        db.add(document); db.flush()
        services.replace_document_chunks(db, document, LEGAL_TEXT)
        services.sync_legal_document_graph(db, document)
        linked = services.link_provisions_to_chunks(db, document)
        db.commit()
        assert linked == 1
        provision = db.query(Entity).filter_by(entity_type="Provision").filter(
            Entity.identity_key.like(f"legal:provision:{document.id}:%")
        ).first()
        assert provision.attributes.get("chunk_ids")
        linked_chunk = db.get(DocumentChunk, provision.attributes["chunk_ids"][0])
        assert linked_chunk.section_number == "15 ทวิ"


def test_link_provisions_to_chunks_is_a_no_op_for_general_documents():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="chunking-general-noop", name="Chunking general noop")
        db.add(kb); db.flush()
        document = Document(knowledge_base_id=kb.id, original_filename="memo.txt", stored_filename="memo.txt",
                            storage_path="/tmp/memo.txt", mime_type="text/plain", file_size=1, checksum_sha256="4" * 64,
                            title="Memo", document_type="general", status="completed")
        db.add(document); db.flush()
        db.commit()
        assert services.link_provisions_to_chunks(db, document) == 0


def test_legal_document_upload_is_chunked_by_section_through_the_real_pipeline():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Chunk E2E", "code": "chunk-e2e"}).json()
    uploaded = test_client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={"file": ("law.txt", LEGAL_TEXT.encode("utf-8"), "text/plain")},
        data={"document_type": "legal"},
    ).json()
    assert test_client.post("/api/v1/internal/process-next").json()["processed"] is True
    # A "legal" document_type upload auto-queues a follow-up EXTRACT_LEGAL_METADATA
    # job; drain it too so it can't leak into a later test's process-next call.
    while test_client.post("/api/v1/internal/process-next").json()["processed"]:
        pass
    with SessionLocal() as db:
        chunks = db.query(DocumentChunk).filter_by(document_id=uploaded["document_id"]).order_by(DocumentChunk.chunk_index).all()
        assert chunks
        section_kinds = {chunk.section_kind for chunk in chunks}
        assert "มาตรา" in section_kinds
        assert any(chunk.section_number == "15 ทวิ" for chunk in chunks)
