import os
import tempfile
from datetime import date

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
from app.services import compose_cited_answer
from app.db import Base, SessionLocal, engine
from app.legal_resolver import resolve_legal_context
from app.main import app
from app.models import Document, DocumentChunk, KnowledgeBase, LegalFamily, LegalInstrument, LegalInstrumentRelation
from app.planner import LegalContext, RetrievalPlan, RetrievalPolicy
from app.retrieval import RetrievalEvidence
from app.schemas import QueryFilters

Base.metadata.create_all(engine)


def client():
    with TestClient(app) as test_client:
        assert test_client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        yield test_client


def _plan(**overrides) -> RetrievalPlan:
    return RetrievalPlan(intent="legal_provision", **overrides)


def source(document_id: str, chunk_id: str, relevance: float = 1.0, section_number=None, section_label=None,
          section_kind=None, title=None) -> dict:
    return {"document_id": document_id, "title": title or document_id, "chunk_id": chunk_id, "excerpt": "evidence",
            "relevance": relevance, "section_number": section_number, "section_label": section_label, "section_kind": section_kind}


# --- resolve_legal_context -------------------------------------------------

def test_resolve_legal_context_returns_none_when_kb_has_no_registry():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolve-none", name="Resolve none")
        db.add(kb); db.flush(); db.commit()
        assert resolve_legal_context(db, "มาตรา 15", [kb.id], _plan(), RetrievalPolicy()) is None


def test_resolve_legal_context_returns_none_when_legal_awareness_disabled():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolve-disabled", name="Resolve disabled")
        db.add(kb); db.flush()
        doc = Document(knowledge_base_id=kb.id, original_filename="a.txt", stored_filename="a.txt", storage_path="/tmp/a.txt",
                      mime_type="text/plain", file_size=1, checksum_sha256="d1" * 32, title="Act", document_type="legal", status="completed")
        db.add(doc); db.flush()
        db.add(LegalInstrument(document_id=doc.id, knowledge_base_id=kb.id, effective_from=date(2020, 1, 1)))
        db.commit()
        assert resolve_legal_context(db, "มาตรา 15", [kb.id], _plan(), RetrievalPolicy(legal_awareness=False)) is None


def test_resolve_legal_context_selects_current_version_by_as_of_date():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolve-version", name="Resolve version")
        db.add(kb); db.flush()
        family = LegalFamily(knowledge_base_id=kb.id, base_title="พระราชบัญญัติทดสอบ", normalized_key="พระราชบัญญัติทดสอบ")
        db.add(family); db.flush()
        base_doc = Document(knowledge_base_id=kb.id, original_filename="base.txt", stored_filename="base.txt", storage_path="/tmp/base.txt",
                            mime_type="text/plain", file_size=1, checksum_sha256="d2" * 32, title="พระราชบัญญัติทดสอบ พ.ศ. 2541",
                            document_type="legal", status="completed")
        amend_doc = Document(knowledge_base_id=kb.id, original_filename="amend.txt", stored_filename="amend.txt", storage_path="/tmp/amend.txt",
                             mime_type="text/plain", file_size=1, checksum_sha256="d3" * 32, title="พระราชบัญญัติทดสอบ (ฉบับที่ 2) พ.ศ. 2562",
                             document_type="legal", status="completed")
        db.add_all([base_doc, amend_doc]); db.flush()
        base_row = LegalInstrument(document_id=base_doc.id, knowledge_base_id=kb.id, family_id=family.id,
                                   official_title=base_doc.title, effective_from=date(1998, 9, 1), status="amended")
        amend_row = LegalInstrument(document_id=amend_doc.id, knowledge_base_id=kb.id, family_id=family.id,
                                    official_title=amend_doc.title, effective_from=date(2019, 5, 1), status="in_force")
        db.add_all([base_row, amend_row]); db.flush()
        db.add(LegalInstrumentRelation(knowledge_base_id=kb.id, source_instrument_id=amend_row.id, target_instrument_id=base_row.id,
                                       relation="AMENDS", review_status="verified"))
        db.commit()

        # Querying today: both the base act and its amendment should be current.
        context = resolve_legal_context(db, "พระราชบัญญัติทดสอบ มาตรา 15", [kb.id], _plan(), RetrievalPolicy())
        assert base_doc.id in context.current_version_ids
        assert amend_doc.id in context.current_version_ids
        assert amend_doc.id in context.amending_instrument_ids

        # Querying as of a date before the amendment: only the base act applies.
        context_past = resolve_legal_context(db, "พระราชบัญญัติทดสอบ", [kb.id], _plan(as_of_date=date(2010, 1, 1)), RetrievalPolicy())
        assert base_doc.id in context_past.current_version_ids
        assert amend_doc.id not in context_past.current_version_ids


def test_resolve_legal_context_prefers_publisher_latest_role_over_another_valid_consolidation():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolve-latest-role", name="Resolve latest role")
        db.add(kb); db.flush()
        family = LegalFamily(knowledge_base_id=kb.id, base_title="ประมวลกฎหมายทดสอบ", normalized_key="ประมวลกฎหมายทดสอบ")
        db.add(family); db.flush()
        old_doc = Document(knowledge_base_id=kb.id, original_filename="old.txt", stored_filename="old.txt", storage_path="/tmp/old.txt",
                           mime_type="text/plain", file_size=1, checksum_sha256="ra" * 32, title="ฉบับปรับปรุง", document_type="legal", status="completed")
        latest_doc = Document(knowledge_base_id=kb.id, original_filename="latest.txt", stored_filename="latest.txt", storage_path="/tmp/latest.txt",
                              mime_type="text/plain", file_size=1, checksum_sha256="rb" * 32, title="ฉบับปรับปรุงล่าสุด", document_type="legal", status="completed")
        db.add_all([old_doc, latest_doc]); db.flush()
        db.add_all([
            LegalInstrument(document_id=old_doc.id, knowledge_base_id=kb.id, family_id=family.id, official_title=old_doc.title,
                            document_class="consolidated", version_role="consolidated", effective_from=date(2010, 1, 1), status="in_force"),
            LegalInstrument(document_id=latest_doc.id, knowledge_base_id=kb.id, family_id=family.id, official_title=latest_doc.title,
                            document_class="consolidated", version_role="latest_consolidated", effective_from=date(2019, 1, 1), status="in_force"),
        ])
        db.commit()
        context = resolve_legal_context(db, "ประมวลกฎหมายทดสอบ มาตรา 5", [kb.id], _plan(), RetrievalPolicy())
        assert context.preferred_document_ids == [latest_doc.id]


def test_direct_current_shortcuts_do_not_bypass_explicit_filters_or_a_historical_year():
    assert services.allows_default_current_direct_path("มาตรา 104") is True
    assert services.allows_default_current_direct_path("มาตรา 104", QueryFilters(as_of_date=date(2010, 1, 1))) is False
    assert services.allows_default_current_direct_path("มาตรา 104", QueryFilters(include_historical=True)) is False
    assert services.allows_default_current_direct_path("มาตรา 104 พ.ศ. 2543") is False


def test_court_decision_scope_check_respects_available_corpus_material():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="court-evidence", name="Court evidence")
        db.add(kb); db.flush()
        db.add(Document(knowledge_base_id=kb.id, original_filename="judgment.txt", stored_filename="judgment.txt",
                        storage_path="/tmp/judgment.txt", mime_type="text/plain", file_size=1,
                        checksum_sha256="court" * 16, title="คำพิพากษาศาลฎีกา", document_type="general", status="completed"))
        db.commit()
        assert services.has_court_decision_evidence(db, [kb.id]) is True


def test_resolve_legal_context_exclusion_sweep_hides_repealed_instrument_without_explicit_mention():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolve-sweep", name="Resolve sweep")
        db.add(kb); db.flush()
        repealed_doc = Document(knowledge_base_id=kb.id, original_filename="old.txt", stored_filename="old.txt", storage_path="/tmp/old.txt",
                                mime_type="text/plain", file_size=1, checksum_sha256="d4" * 32, title="Old notice",
                                document_type="regulation", status="completed")
        db.add(repealed_doc); db.flush()
        db.add(LegalInstrument(document_id=repealed_doc.id, knowledge_base_id=kb.id, effective_from=date(2010, 1, 1), status="repealed"))
        db.commit()
        context = resolve_legal_context(db, "unrelated question about something else entirely", [kb.id], _plan(), RetrievalPolicy())
        assert repealed_doc.id in context.excluded_document_ids


def test_resolve_legal_context_include_historical_disables_exclusion():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolve-historical", name="Resolve historical")
        db.add(kb); db.flush()
        repealed_doc = Document(knowledge_base_id=kb.id, original_filename="old2.txt", stored_filename="old2.txt", storage_path="/tmp/old2.txt",
                                mime_type="text/plain", file_size=1, checksum_sha256="d5" * 32, title="Old notice 2",
                                document_type="regulation", status="completed")
        db.add(repealed_doc); db.flush()
        db.add(LegalInstrument(document_id=repealed_doc.id, knowledge_base_id=kb.id, effective_from=date(2010, 1, 1), status="repealed"))
        db.commit()
        context = resolve_legal_context(db, "anything", [kb.id], _plan(include_historical=True), RetrievalPolicy())
        assert context.excluded_document_ids == []


def test_resolve_legal_context_respects_exclude_invalid_false():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolve-no-exclude", name="Resolve no exclude")
        db.add(kb); db.flush()
        repealed_doc = Document(knowledge_base_id=kb.id, original_filename="old3.txt", stored_filename="old3.txt", storage_path="/tmp/old3.txt",
                                mime_type="text/plain", file_size=1, checksum_sha256="d6" * 32, title="Old notice 3",
                                document_type="regulation", status="completed")
        db.add(repealed_doc); db.flush()
        db.add(LegalInstrument(document_id=repealed_doc.id, knowledge_base_id=kb.id, effective_from=date(2010, 1, 1), status="repealed"))
        db.commit()
        context = resolve_legal_context(db, "anything", [kb.id], _plan(), RetrievalPolicy(exclude_invalid=False))
        assert context.excluded_document_ids == []


def test_provision_query_prefers_current_consolidated_expression_for_short_family_alias():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolve-consolidated", name="Resolve consolidated")
        db.add(kb); db.flush()
        family = LegalFamily(knowledge_base_id=kb.id, base_title="ประมวลกฎหมายที่ดิน", normalized_key="ประมวลกฎหมายที่ดิน")
        db.add(family); db.flush()
        current_doc = Document(knowledge_base_id=kb.id, original_filename="latest.txt", stored_filename="latest.txt",
                               storage_path="/tmp/latest.txt", mime_type="text/plain", file_size=1,
                               checksum_sha256="e1" * 32, title="ฉบับปรับปรุงล่าสุด", document_type="legal", status="completed")
        amendment_doc = Document(knowledge_base_id=kb.id, original_filename="amend.txt", stored_filename="amend.txt",
                                  storage_path="/tmp/amend.txt", mime_type="text/plain", file_size=1,
                                  checksum_sha256="e2" * 32, title="ฉบับแก้ไข ครั้งที่ 11", document_type="legal", status="completed")
        old_doc = Document(knowledge_base_id=kb.id, original_filename="old.txt", stored_filename="old.txt",
                           storage_path="/tmp/old.txt", mime_type="text/plain", file_size=1,
                           checksum_sha256="e3" * 32, title="ฉบับปรับปรุงเก่า", document_type="legal", status="completed")
        db.add_all([current_doc, amendment_doc, old_doc]); db.flush()
        db.add_all([
            LegalInstrument(document_id=current_doc.id, knowledge_base_id=kb.id, family_id=family.id,
                            official_title=current_doc.title, document_class="consolidated", status="in_force",
                            effective_from=date(2019, 11, 21)),
            LegalInstrument(document_id=amendment_doc.id, knowledge_base_id=kb.id, family_id=family.id,
                            official_title=amendment_doc.title, document_class="amendment", status="in_force",
                            effective_from=date(2008, 2, 7)),
            LegalInstrument(document_id=old_doc.id, knowledge_base_id=kb.id, family_id=family.id,
                            official_title=old_doc.title, document_class="consolidated", status="superseded",
                            effective_from=date(2008, 2, 7), effective_to=date(2019, 11, 21)),
        ])
        db.commit()

        context = resolve_legal_context(db, "กรมที่ดิน มาตรา ๖ ใจความสำคัญ", [kb.id], _plan(), RetrievalPolicy())
        assert context.ambiguous_context is False
        assert context.preferred_document_ids == [current_doc.id]
        assert context.current_version_ids == [current_doc.id]
        assert old_doc.id in context.excluded_document_ids
        assert amendment_doc.id not in context.preferred_document_ids


def test_provision_query_fails_closed_when_two_current_consolidated_expressions_match():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolve-ambiguous", name="Resolve ambiguous")
        db.add(kb); db.flush()
        family = LegalFamily(knowledge_base_id=kb.id, base_title="กฎหมายทดสอบ", normalized_key="กฎหมายทดสอบ")
        db.add(family); db.flush()
        docs = []
        for index in (1, 2):
            doc = Document(knowledge_base_id=kb.id, original_filename=f"ambiguous-{index}.txt",
                           stored_filename=f"ambiguous-{index}.txt", storage_path=f"/tmp/ambiguous-{index}.txt",
                           mime_type="text/plain", file_size=1, checksum_sha256=f"f{index}" * 64,
                           title=f"ฉบับรวม {index}", document_type="legal", status="completed")
            docs.append(doc); db.add(doc); db.flush()
            db.add(LegalInstrument(document_id=doc.id, knowledge_base_id=kb.id, family_id=family.id,
                                   official_title=doc.title, document_class="consolidated", status="in_force",
                                   effective_from=date(2020 + index, 1, 1)))
        db.commit()

        context = resolve_legal_context(db, "กฎหมายทดสอบ มาตรา ๖", [kb.id], _plan(), RetrievalPolicy())
        assert context.ambiguous_context is True
        assert len(context.candidate_instrument_ids) == 2
        assert context.matched_instrument_ids == []


def test_consolidated_provision_selector_ignores_enabling_act_and_appendix_duplicates():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="body-selector", name="Body selector")
        db.add(kb); db.flush()
        doc = Document(knowledge_base_id=kb.id, original_filename="latest.txt", stored_filename="latest.txt",
                       storage_path="/tmp/latest.txt", mime_type="text/plain", file_size=1,
                       checksum_sha256="b1" * 32, title="ฉบับปรับปรุงล่าสุด", document_type="legal", status="completed")
        db.add(doc); db.flush()
        db.add_all([
            DocumentChunk(document_id=doc.id, knowledge_base_id=kb.id, chunk_index=7, content="พระราชบัญญัติ มาตรา ๖",
                          content_sha256="c1" * 32, char_start=0, char_end=10, token_count=3,
                          section_kind="มาตรา", section_number="6"),
            DocumentChunk(document_id=doc.id, knowledge_base_id=kb.id, chunk_index=17, content="หมวด ๑",
                          content_sha256="c2" * 32, char_start=11, char_end=20, token_count=2,
                          section_kind="หมวด"),
            DocumentChunk(document_id=doc.id, knowledge_base_id=kb.id, chunk_index=25, content="ประมวลกฎหมายที่ดิน มาตรา ๖",
                          content_sha256="c3" * 32, char_start=21, char_end=40, token_count=5,
                          section_kind="มาตรา", section_number="6"),
            DocumentChunk(document_id=doc.id, knowledge_base_id=kb.id, chunk_index=243, content="ภาคผนวกแก้ไข มาตรา ๖",
                          content_sha256="c4" * 32, char_start=41, char_end=60, token_count=5,
                          section_kind="มาตรา", section_number="6"),
        ])
        db.commit()
        plan = _plan(legal_context=LegalContext(preferred_document_ids=[doc.id], provision_refs=["มาตรา ๖"]))
        channels = [RetrievalEvidence([
            {**source(doc.id, "appendix", section_number="6", section_kind="มาตรา"), "chunk_index": 243},
            {**source(doc.id, "body", section_number="6", section_kind="มาตรา"), "chunk_index": 25},
            {**source(doc.id, "enabling", section_number="6", section_kind="มาตรา"), "chunk_index": 7},
        ], [], [], [])]

        filtered = services._prefer_consolidated_body_sources(db, channels, plan, [])
        assert [row["chunk_id"] for row in filtered[0].sources] == ["body"]


def test_answer_citations_drop_unused_candidates_and_fail_closed_on_missing_citation():
    evidence = RetrievalEvidence([
        {**source("doc-a", "c1"), "citation_id": "S1"}, {**source("doc-b", "c2"), "citation_id": "S2"}
    ], [], [], [], "คำตอบ [S2]")
    services._validate_answer_citations(evidence, [])
    assert [item["citation_id"] for item in evidence.sources] == ["S2"]

    invalid = RetrievalEvidence([{**source("doc-a", "c1"), "citation_id": "S1"}], [], [], [], "คำตอบไม่มีการอ้างอิง")
    warnings = []
    services._validate_answer_citations(invalid, warnings)
    # F6: the unverifiable answer is dropped, but the evidence is KEPT — the
    # composed answer falls back to listing it instead of denying everything.
    assert invalid.answer is None
    assert [item["citation_id"] for item in invalid.sources] == ["S1"]
    assert any(item["code"] == "ANSWER_CITATIONS_MISSING" for item in warnings)
    fallback = compose_cited_answer(invalid, warnings)
    assert "พบหลักฐานที่เกี่ยวข้อง [S1]" in fallback


# --- fuse_evidence weighting ------------------------------------------------

def test_fuse_evidence_without_legal_meta_matches_plain_rrf_exactly():
    semantic = RetrievalEvidence([source("doc-a", "c1"), source("doc-b", "c2")], [], [], [])
    full_text = RetrievalEvidence([source("doc-b", "c2"), source("doc-a", "c1")], [], [], [])
    plain = services.fuse_evidence(semantic, full_text, limit=2)
    weighted_but_empty_meta = services.fuse_evidence(semantic, full_text, limit=2, legal_meta={},
                                                      authority_weight=0.3, status_weight=0.35, recency_weight=0.15)
    assert plain.sources == weighted_but_empty_meta.sources


def test_fuse_evidence_authority_boost_reorders_equal_rrf_candidates():
    channel = RetrievalEvidence([source("doc-faq", "c1"), source("doc-act", "c2")], [], [], [])
    legal_meta = {
        "doc-faq": {"authority_level": 20, "status": "in_force", "effective_from": date(2020, 1, 1)},
        "doc-act": {"authority_level": 90, "status": "in_force", "effective_from": date(2020, 1, 1)},
    }
    result = services.fuse_evidence(channel, limit=2, legal_meta=legal_meta, authority_weight=0.5, status_weight=0.0, recency_weight=0.0)
    assert [item["document_id"] for item in result.sources] == ["doc-act", "doc-faq"]


def test_fuse_evidence_status_weight_penalizes_repealed_document():
    channel = RetrievalEvidence([source("doc-repealed", "c1"), source("doc-current", "c2")], [], [], [])
    legal_meta = {
        "doc-repealed": {"authority_level": 90, "status": "repealed", "effective_from": date(2000, 1, 1)},
        "doc-current": {"authority_level": 90, "status": "in_force", "effective_from": date(2020, 1, 1)},
    }
    result = services.fuse_evidence(channel, limit=2, legal_meta=legal_meta, authority_weight=0.0, status_weight=0.5, recency_weight=0.0)
    assert result.sources[0]["document_id"] == "doc-current"


def test_fuse_evidence_guarantees_current_version_representation():
    # doc-current never surfaces on raw similarity (rank far outside the limit)
    # but must still appear because it's the resolver's current version.
    sources_list = [source(f"doc-{i}", f"c{i}") for i in range(20)] + [source("doc-current", "c-current")]
    channel = RetrievalEvidence(sources_list, [], [], [])
    legal_context = LegalContext(current_version_ids=["doc-current"])
    result = services.fuse_evidence(channel, limit=5, legal_context=legal_context)
    assert "doc-current" in {item["document_id"] for item in result.sources}


def test_apply_current_version_guarantee_restores_a_document_the_reranker_dropped():
    # Simulates rerank_evidence returning a purely text-similarity-ranked list
    # that dropped the resolver's current version; the pool (pre-rerank fused
    # ranking) still has it and it must be restored, capped at limit.
    reranked = [source(f"doc-{i}", f"c{i}") for i in range(5)]
    pool = reranked + [source("doc-current", "c-current")]
    legal_context = LegalContext(current_version_ids=["doc-current"])
    result = services._apply_current_version_guarantee(reranked, legal_context, limit=5, pool=pool)
    assert len(result) == 5
    assert "doc-current" in {item["document_id"] for item in result}


def test_apply_current_version_guarantee_is_a_no_op_without_legal_context():
    reranked = [source(f"doc-{i}", f"c{i}") for i in range(3)]
    result = services._apply_current_version_guarantee(reranked, None, limit=5, pool=reranked)
    assert result == reranked


def test_fuse_evidence_backfill_never_exceeds_limit():
    # 8 current-version documents all miss the raw top-5, so a naive backfill
    # would return 8 sources for a limit of 5.
    sources_list = [source(f"doc-{i}", f"c{i}") for i in range(20)]
    current_ids = [f"missing-{i}" for i in range(8)]
    sources_list += [source(doc_id, f"c-{doc_id}") for doc_id in current_ids]
    channel = RetrievalEvidence(sources_list, [], [], [])
    legal_context = LegalContext(current_version_ids=current_ids)
    result = services.fuse_evidence(channel, limit=5, legal_context=legal_context)
    assert len(result.sources) == 5


# --- validate_legal_evidence -------------------------------------------------

def test_validate_legal_evidence_removes_superseded_duplicate_provision():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="validate-dup", name="Validate dup")
        db.add(kb); db.flush()
        family = LegalFamily(knowledge_base_id=kb.id, base_title="Test act", normalized_key="test act")
        db.add(family); db.flush()
        old_doc = Document(knowledge_base_id=kb.id, original_filename="old.txt", stored_filename="old.txt", storage_path="/tmp/old.txt",
                           mime_type="text/plain", file_size=1, checksum_sha256="e1" * 32, title="Old act", document_type="legal", status="completed")
        new_doc = Document(knowledge_base_id=kb.id, original_filename="new.txt", stored_filename="new.txt", storage_path="/tmp/new.txt",
                           mime_type="text/plain", file_size=1, checksum_sha256="e2" * 32, title="New act", document_type="legal", status="completed")
        db.add_all([old_doc, new_doc]); db.flush()
        old_row = LegalInstrument(document_id=old_doc.id, knowledge_base_id=kb.id, family_id=family.id, official_title="Old act",
                                  effective_from=date(2000, 1, 1), status="amended")
        new_row = LegalInstrument(document_id=new_doc.id, knowledge_base_id=kb.id, family_id=family.id, official_title="New act",
                                  effective_from=date(2020, 1, 1), status="in_force")
        db.add_all([old_row, new_row]); db.flush()
        db.add(LegalInstrumentRelation(knowledge_base_id=kb.id, source_instrument_id=new_row.id, target_instrument_id=old_row.id,
                                       relation="AMENDS", target_provision="15", review_status="verified"))
        db.commit()

        evidence = RetrievalEvidence([
            source(old_doc.id, "old-c1", section_number="15", title="Old act"),
            source(new_doc.id, "new-c1", section_number="15", title="New act"),
        ], [], [], [])
        plan = _plan(legal_context=LegalContext(current_version_ids=[new_doc.id]))
        warnings = services.validate_legal_evidence(db, evidence, plan, {old_doc.id: old_row, new_doc.id: new_row})
        assert [item["document_id"] for item in evidence.sources] == [new_doc.id]
        assert any(w["code"] == "SUPERSEDED_VERSION_REMOVED" for w in warnings)


def test_validate_legal_evidence_does_not_confuse_a_section_heading_with_a_provision_of_the_same_number():
    # A single document's own "หมวด 2" (chapter heading) and "มาตรา 2" (article)
    # both normalize to section_number="2" but are unrelated provisions from the
    # SAME document -- they must never be treated as a version conflict.
    with SessionLocal() as db:
        kb = KnowledgeBase(code="validate-section-kind", name="Validate section kind")
        db.add(kb); db.flush()
        family = LegalFamily(knowledge_base_id=kb.id, base_title="Single act", normalized_key="single act")
        db.add(family); db.flush()
        doc = Document(knowledge_base_id=kb.id, original_filename="single.txt", stored_filename="single.txt", storage_path="/tmp/single.txt",
                       mime_type="text/plain", file_size=1, checksum_sha256="e9" * 32, title="Single act", document_type="legal", status="completed")
        db.add(doc); db.flush()
        row = LegalInstrument(document_id=doc.id, knowledge_base_id=kb.id, family_id=family.id, official_title="Single act",
                              effective_from=date(2020, 1, 1), status="in_force")
        db.add(row); db.commit()

        evidence = RetrievalEvidence([
            source(doc.id, "c-chapter", section_number="2", section_kind="หมวด", section_label="หมวด 2"),
            source(doc.id, "c-article", section_number="2", section_kind="มาตรา", section_label="มาตรา 2"),
        ], [], [], [])
        plan = _plan(legal_context=LegalContext(current_version_ids=[doc.id]))
        warnings = services.validate_legal_evidence(db, evidence, plan, {doc.id: row})
        assert len(evidence.sources) == 2
        assert warnings == []


def test_validate_legal_evidence_does_not_treat_a_long_provisions_own_sub_chunks_as_a_conflict():
    # A provision long enough to be sub-split into multiple chunks shares the
    # same (family, section_kind, section_number) across chunks of the SAME
    # document; that must never be treated as a version conflict either.
    with SessionLocal() as db:
        kb = KnowledgeBase(code="validate-sub-chunks", name="Validate sub chunks")
        db.add(kb); db.flush()
        family = LegalFamily(knowledge_base_id=kb.id, base_title="Long act", normalized_key="long act")
        db.add(family); db.flush()
        doc = Document(knowledge_base_id=kb.id, original_filename="long.txt", stored_filename="long.txt", storage_path="/tmp/long.txt",
                       mime_type="text/plain", file_size=1, checksum_sha256="e8" * 32, title="Long act", document_type="legal", status="completed")
        db.add(doc); db.flush()
        row = LegalInstrument(document_id=doc.id, knowledge_base_id=kb.id, family_id=family.id, official_title="Long act",
                              effective_from=date(2020, 1, 1), status="in_force")
        db.add(row); db.commit()

        evidence = RetrievalEvidence([
            source(doc.id, "c-part1", section_number="9", section_kind="มาตรา", section_label="มาตรา 9"),
            source(doc.id, "c-part2", section_number="9", section_kind="มาตรา", section_label="มาตรา 9"),
        ], [], [], [])
        plan = _plan(legal_context=LegalContext(current_version_ids=[doc.id]))
        warnings = services.validate_legal_evidence(db, evidence, plan, {doc.id: row})
        assert len(evidence.sources) == 2
        assert warnings == []


def test_validate_legal_evidence_keeps_both_versions_when_include_historical():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="validate-historical", name="Validate historical")
        db.add(kb); db.flush()
        family = LegalFamily(knowledge_base_id=kb.id, base_title="Test act 2", normalized_key="test act 2")
        db.add(family); db.flush()
        old_doc = Document(knowledge_base_id=kb.id, original_filename="old2.txt", stored_filename="old2.txt", storage_path="/tmp/old2.txt",
                           mime_type="text/plain", file_size=1, checksum_sha256="e3" * 32, title="Old act 2", document_type="legal", status="completed")
        new_doc = Document(knowledge_base_id=kb.id, original_filename="new2.txt", stored_filename="new2.txt", storage_path="/tmp/new2.txt",
                           mime_type="text/plain", file_size=1, checksum_sha256="e4" * 32, title="New act 2", document_type="legal", status="completed")
        db.add_all([old_doc, new_doc]); db.flush()
        old_row = LegalInstrument(document_id=old_doc.id, knowledge_base_id=kb.id, family_id=family.id, effective_from=date(2000, 1, 1))
        new_row = LegalInstrument(document_id=new_doc.id, knowledge_base_id=kb.id, family_id=family.id, effective_from=date(2020, 1, 1))
        db.add_all([old_row, new_row]); db.commit()

        evidence = RetrievalEvidence([
            source(old_doc.id, "old-c1", section_number="9"),
            source(new_doc.id, "new-c1", section_number="9"),
        ], [], [], [])
        plan = _plan(include_historical=True)
        warnings = services.validate_legal_evidence(db, evidence, plan, {old_doc.id: old_row, new_doc.id: new_row})
        assert len(evidence.sources) == 2
        assert any(w["code"] == "SUPERSEDED_VERSION_PRESENT" for w in warnings)


def test_validate_legal_evidence_flags_unverified_validity():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="validate-unverified", name="Validate unverified")
        db.add(kb); db.flush()
        doc = Document(knowledge_base_id=kb.id, original_filename="u.txt", stored_filename="u.txt", storage_path="/tmp/u.txt",
                       mime_type="text/plain", file_size=1, checksum_sha256="e5" * 32, title="Unclear act", document_type="legal", status="completed")
        db.add(doc); db.flush()
        row = LegalInstrument(document_id=doc.id, knowledge_base_id=kb.id, status="unknown")
        db.add(row); db.commit()
        evidence = RetrievalEvidence([source(doc.id, "c1")], [], [], [])
        warnings = services.validate_legal_evidence(db, evidence, _plan(), {doc.id: row})
        assert any(w["code"] == "UNVERIFIED_VALIDITY" for w in warnings)


def test_decorate_sources_with_legal_metadata_adds_status_and_label():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="decorate", name="Decorate")
        db.add(kb); db.flush()
        doc = Document(knowledge_base_id=kb.id, original_filename="d.txt", stored_filename="d.txt", storage_path="/tmp/d.txt",
                       mime_type="text/plain", file_size=1, checksum_sha256="e6" * 32, title="Decorated act", document_type="legal", status="completed")
        db.add(doc); db.flush()
        row = LegalInstrument(document_id=doc.id, knowledge_base_id=kb.id, official_title="Decorated act", kind="act",
                              authority_level=90, status="in_force", effective_from=date(2020, 1, 1))
        db.add(row); db.commit()
        sources = [source(doc.id, "c1", section_label="มาตรา 5")]
        services._decorate_sources_with_legal_metadata(sources, {doc.id: row})
        assert sources[0]["document_status"] == "in_force"
        assert sources[0]["authority_level"] == 90
        assert "บังคับใช้" in sources[0]["legal_label"]
        assert "มาตรา 5" in sources[0]["legal_label"]


# --- end-to-end through the query endpoint ----------------------------------

def test_query_endpoint_prefers_current_version_and_reports_conflict_warning():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Legal retrieval E2E", "code": "legal-retrieval-e2e"}).json()
    with SessionLocal() as db:
        family = LegalFamily(knowledge_base_id=kb["id"], base_title="พระราชบัญญัติค่าชดเชย", normalized_key="พระราชบัญญัติค่าชดเชย")
        db.add(family); db.flush()
        old_text = "มาตรา 15 ค่าชดเชยเดิมเท่ากับค่าจ้างหกสิบวัน"
        new_text = "มาตรา 15 ค่าชดเชยใหม่เท่ากับค่าจ้างเก้าสิบวัน"
        old_doc = Document(knowledge_base_id=kb["id"], original_filename="old.txt", stored_filename="old.txt", storage_path="/tmp/old.txt",
                           mime_type="text/plain", file_size=1, checksum_sha256="f1" * 32, title="พระราชบัญญัติค่าชดเชย พ.ศ. 2541",
                           document_type="legal", status="completed", extracted_text=old_text)
        new_doc = Document(knowledge_base_id=kb["id"], original_filename="new.txt", stored_filename="new.txt", storage_path="/tmp/new.txt",
                           mime_type="text/plain", file_size=1, checksum_sha256="f2" * 32, title="พระราชบัญญัติค่าชดเชย (ฉบับที่ 2) พ.ศ. 2562",
                           document_type="legal", status="completed", extracted_text=new_text)
        db.add_all([old_doc, new_doc]); db.flush()
        services.replace_document_chunks(db, old_doc, old_text)
        services.replace_document_chunks(db, new_doc, new_text)
        old_row = LegalInstrument(document_id=old_doc.id, knowledge_base_id=kb["id"], family_id=family.id,
                                  official_title=old_doc.title, authority_level=90, kind="act",
                                  effective_from=date(1998, 9, 1), status="amended")
        new_row = LegalInstrument(document_id=new_doc.id, knowledge_base_id=kb["id"], family_id=family.id,
                                  official_title=new_doc.title, authority_level=90, kind="act",
                                  effective_from=date(2019, 5, 1), status="in_force")
        db.add_all([old_row, new_row]); db.flush()
        db.add(LegalInstrumentRelation(knowledge_base_id=kb["id"], source_instrument_id=new_row.id, target_instrument_id=old_row.id,
                                       relation="AMENDS", target_provision="15", review_status="verified"))
        db.commit()

    response = test_client.post("/api/v1/query", json={"query": "มาตรา 15 ค่าชดเชยเท่าไร", "knowledge_base_ids": [kb["id"]], "max_sources": 10})
    assert response.status_code == 200
    payload = response.json()
    document_ids = {source_item["document_id"] for source_item in payload["sources"]}
    with SessionLocal() as db:
        old_doc_id = db.query(Document).filter_by(knowledge_base_id=kb["id"], checksum_sha256="f1" * 32).first().id
        new_doc_id = db.query(Document).filter_by(knowledge_base_id=kb["id"], checksum_sha256="f2" * 32).first().id
    assert new_doc_id in document_ids
    assert old_doc_id not in document_ids
    assert any(w["code"] == "SUPERSEDED_VERSION_REMOVED" for w in payload["warnings"])
    kept_source = next(s for s in payload["sources"] if s["document_id"] == new_doc_id)
    assert kept_source["document_status"] == "in_force"
    assert "บังคับใช้" in kept_source["legal_label"]


def test_query_endpoint_as_of_past_date_returns_the_earlier_version():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Legal as-of E2E", "code": "legal-as-of-e2e"}).json()
    with SessionLocal() as db:
        family = LegalFamily(knowledge_base_id=kb["id"], base_title="พระราชบัญญัติเวลา", normalized_key="พระราชบัญญัติเวลา")
        db.add(family); db.flush()
        old_text = "มาตรา 9 กำหนดวิธีเดิม"
        new_text = "มาตรา 9 กำหนดวิธีใหม่"
        old_doc = Document(knowledge_base_id=kb["id"], original_filename="old3.txt", stored_filename="old3.txt", storage_path="/tmp/old3.txt",
                           mime_type="text/plain", file_size=1, checksum_sha256="f3" * 32, title="พระราชบัญญัติเวลา พ.ศ. 2540",
                           document_type="legal", status="completed", extracted_text=old_text)
        new_doc = Document(knowledge_base_id=kb["id"], original_filename="new3.txt", stored_filename="new3.txt", storage_path="/tmp/new3.txt",
                           mime_type="text/plain", file_size=1, checksum_sha256="f4" * 32, title="พระราชบัญญัติเวลา (ฉบับที่ 2) พ.ศ. 2564",
                           document_type="legal", status="completed", extracted_text=new_text)
        db.add_all([old_doc, new_doc]); db.flush()
        services.replace_document_chunks(db, old_doc, old_text)
        services.replace_document_chunks(db, new_doc, new_text)
        old_row = LegalInstrument(document_id=old_doc.id, knowledge_base_id=kb["id"], family_id=family.id,
                                  official_title=old_doc.title, authority_level=90, effective_from=date(1997, 1, 1), status="amended")
        new_row = LegalInstrument(document_id=new_doc.id, knowledge_base_id=kb["id"], family_id=family.id,
                                  official_title=new_doc.title, authority_level=90, effective_from=date(2021, 1, 1), status="in_force")
        db.add_all([old_row, new_row]); db.flush()
        db.add(LegalInstrumentRelation(knowledge_base_id=kb["id"], source_instrument_id=new_row.id, target_instrument_id=old_row.id,
                                       relation="AMENDS", target_provision="9", review_status="verified"))
        db.commit()

    response = test_client.post("/api/v1/query", json={
        "query": "มาตรา 9", "knowledge_base_ids": [kb["id"]], "max_sources": 10,
        "filters": {"as_of_date": "2010-01-01"},
    })
    assert response.status_code == 200
    payload = response.json()
    document_ids = {source_item["document_id"] for source_item in payload["sources"]}
    with SessionLocal() as db:
        old_doc_id = db.query(Document).filter_by(knowledge_base_id=kb["id"], checksum_sha256="f3" * 32).first().id
        new_doc_id = db.query(Document).filter_by(knowledge_base_id=kb["id"], checksum_sha256="f4" * 32).first().id
    assert old_doc_id in document_ids
    assert new_doc_id not in document_ids


def test_query_endpoint_regression_general_knowledge_base_is_unaffected():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "General regression", "code": "general-regression"}).json()
    test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={
        "file": ("architecture.txt", b"Customer Portal runs on APP-01.", "text/plain"),
    }).json()
    assert test_client.post("/api/v1/internal/process-next").json()["processed"] is True
    response = test_client.post("/api/v1/query", json={"query": "What runs on APP-01?", "knowledge_base_ids": [kb["id"]]})
    assert response.status_code == 200
    payload = response.json()
    assert payload["sources"]
    assert payload["warnings"] == []
    assert "legal_label" not in payload["sources"][0]
    assert not any(step["channel"] == "legal_resolver" and step["status"] == "used" for step in payload["metadata"]["retrieval_trace"])


def test_retrieval_config_accepts_legal_weight_and_awareness_fields():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "Legal policy knobs", "code": "legal-policy-knobs"}).json()
    updated = test_client.patch(f"/api/v1/knowledge-bases/{kb['id']}/retrieval-config", json={
        "legal_awareness": False, "exclude_invalid": False,
        "authority_weight": 0.5, "recency_weight": 0.2, "status_weight": 0.1,
    })
    assert updated.status_code == 200
    config = updated.json()["retrieval_config"]
    assert config["legal_awareness"] is False
    assert config["exclude_invalid"] is False
    assert config["authority_weight"] == 0.5
    assert config["recency_weight"] == 0.2
    assert config["status_weight"] == 0.1


def test_mcp_search_knowledge_tool_schema_exposes_legal_filters():
    test_client = next(client())
    kb = test_client.post("/api/v1/knowledge-bases", json={"name": "MCP schema", "code": "mcp-schema-legal"}).json()
    test_client.post(f"/api/v1/knowledge-bases/{kb['id']}/activate")
    token = test_client.post("/api/v1/tokens", json={"name": "agent", "allowed_knowledge_base_ids": [kb["id"]], "allowed_tools": ["search_knowledge"]}).json()
    reply = test_client.post("/mcp", headers={"Authorization": f"Bearer {token['token']}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tool = next(item for item in reply.json()["result"]["tools"] if item["name"] == "search_knowledge")
    filters_schema = tool["inputSchema"]["$defs"]["QueryFilters"]["properties"]
    assert "as_of_date" in filters_schema
    assert "include_historical" in filters_schema


# --- provenance reconciliation and per-provision coverage ------------------

def _provenance_kb(db, code):
    kb = KnowledgeBase(code=code, name=code)
    db.add(kb); db.flush()
    family = LegalFamily(knowledge_base_id=kb.id, base_title="ประมวลกฎหมายที่ดิน", normalized_key="ประมวลกฎหมายที่ดิน")
    db.add(family); db.flush()
    current_doc = Document(knowledge_base_id=kb.id, original_filename="latest.txt", stored_filename="latest.txt",
                           storage_path="/tmp/latest.txt", mime_type="text/plain", file_size=1,
                           checksum_sha256="a1" * 32, title="ฉบับปรับปรุงล่าสุด", document_type="legal", status="completed")
    db.add(current_doc); db.flush()
    current = LegalInstrument(document_id=current_doc.id, knowledge_base_id=kb.id, family_id=family.id,
                              official_title=current_doc.title, document_class="consolidated", status="in_force",
                              version_role="latest_consolidated", effective_from=date(2019, 11, 21))
    db.add(current); db.flush()
    return kb, family, current


def _amending(db, kb, family, *, title, when):
    doc = Document(knowledge_base_id=kb.id, original_filename=f"{title}.txt", stored_filename=f"{title}.txt",
                   storage_path=f"/tmp/{title}.txt", mime_type="text/plain", file_size=1,
                   checksum_sha256=__import__("hashlib").sha256(title.encode()).hexdigest(), title=title,
                   document_type="legal", status="completed")
    db.add(doc); db.flush()
    instrument = LegalInstrument(document_id=doc.id, knowledge_base_id=kb.id, family_id=family.id,
                                 official_title=title, document_class="amendment", status="in_force", version_date=when)
    db.add(instrument); db.flush()
    return instrument


def test_provenance_leads_with_the_repeal_and_flags_a_post_repeal_amend_as_another_work():
    with SessionLocal() as db:
        kb, family, current = _provenance_kb(db, "prov-reconcile")
        repeal = _amending(db, kb, family, title="พระราชบัญญัติแก้ไขเพิ่มเติมประมวลกฎหมายที่ดิน (ฉบับที่ 3) พ.ศ. 2526", when=date(1983, 6, 1))
        amend = _amending(db, kb, family, title="พระราชบัญญัติแก้ไขเพิ่มเติมประมวลกฎหมายที่ดิน (ฉบับที่ 9) พ.ศ. 2543", when=date(2000, 5, 1))
        db.add_all([
            LegalInstrumentRelation(knowledge_base_id=kb.id, source_instrument_id=repeal.id, target_instrument_id=current.id,
                                    relation="REPEALS", target_provision="7", review_status="verified"),
            LegalInstrumentRelation(knowledge_base_id=kb.id, source_instrument_id=amend.id, target_instrument_id=current.id,
                                    relation="AMENDS", target_provision="7", review_status="verified"),
        ])
        db.commit()
        result = services.build_legal_provenance_result(db, "มาตรา 7 มีสถานะอย่างไร และถูกยกเลิกโดยกฎหมายใด", [kb.id])
        answer = result["answer"]
        assert "มาตรา 7 ยกเลิกโดย" in answer
        assert "(ฉบับที่ 3) พ.ศ. 2526" in answer
        # The impossible "amended after repeal" edge is flagged, not emitted as a
        # contradictory peer statement.
        assert "หมายเหตุ" in answer and "คนละฉบับ" in answer
        assert "มาตรา 7 แก้ไขเพิ่มเติมโดย" not in answer


def test_provenance_reports_each_asked_provision_including_one_without_an_edge():
    with SessionLocal() as db:
        kb, family, current = _provenance_kb(db, "prov-per-provision")
        amend = _amending(db, kb, family, title="พระราชบัญญัติแก้ไขเพิ่มเติมประมวลกฎหมายที่ดิน (ฉบับที่ 8) พ.ศ. 2542", when=date(1999, 5, 1))
        db.add(LegalInstrumentRelation(knowledge_base_id=kb.id, source_instrument_id=amend.id, target_instrument_id=current.id,
                                       relation="AMENDS", target_provision="96 ทวิ", review_status="verified"))
        db.commit()
        result = services.build_legal_provenance_result(db, "มาตรา 96 ทวิ และมาตรา 96 ตรี เพิ่มโดยกฎหมายฉบับใด", [kb.id])
        answer = result["answer"]
        assert "มาตรา 96 ทวิ แก้ไขเพิ่มเติมโดย" in answer
        assert "(ฉบับที่ 8) พ.ศ. 2542" in answer
        assert "ไม่พบความสัมพันธ์การแก้ไขหรือยกเลิกที่ตรวจสอบแล้วสำหรับมาตรา 96 ตรี" in answer


# --- trace-explorer observability fields on deterministic legal shortcuts ---

def test_legal_provenance_result_populates_trace_preview_fields():
    with SessionLocal() as db:
        kb, family, current = _provenance_kb(db, "prov-trace-preview")
        amend = _amending(db, kb, family, title="พระราชบัญญัติแก้ไขเพิ่มเติมประมวลกฎหมายที่ดิน (ฉบับที่ 8) พ.ศ. 2542", when=date(1999, 5, 1))
        db.add(LegalInstrumentRelation(knowledge_base_id=kb.id, source_instrument_id=amend.id, target_instrument_id=current.id,
                                       relation="AMENDS", target_provision="96 ทวิ", review_status="verified"))
        db.commit()
        query = "มาตรา 96 ทวิ เพิ่มโดยกฎหมายฉบับใด"
        result = services.build_legal_provenance_result(db, query, [kb.id])
        metadata = result["metadata"]
        # These are the fields the Trace Explorer reads (request_summary /
        # response_summary in record_retrieval_execution); without them the UI
        # falls back to "Query preview unavailable" / "No answer preview" even
        # though the shortcut answered successfully.
        assert metadata["query_preview"] == query
        assert metadata["query_length"] == len(query)
        assert metadata["query_sha256"]
        assert metadata["answer_preview"].startswith("มาตรา 96 ทวิ แก้ไขเพิ่มเติมโดย")
        assert metadata["citation_ids"] == [source["citation_id"] for source in result["sources"]]


def test_persist_legal_clause_result_populates_trace_preview_fields():
    with SessionLocal() as db:
        kb, family, current = _provenance_kb(db, "clause-trace-preview")
        result = services.build_default_current_legal_result(db, "หลักเกณฑ์ค่าธรรมเนียมปัจจุบัน", [kb.id])
        metadata = result["metadata"]
        assert metadata["query_preview"] == "หลักเกณฑ์ค่าธรรมเนียมปัจจุบัน"
        assert metadata["query_sha256"]
        assert "citation_ids" in metadata


def test_persist_legal_clause_result_does_not_attribute_a_different_familys_amendment():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="cross-family-attribution", name="Cross family attribution")
        db.add(kb); db.flush()

        # Family A: the instrument actually being answered about. Its มาตรา 104
        # has no amendment of its own.
        family_a = LegalFamily(knowledge_base_id=kb.id, base_title="กฎหมาย ก", normalized_key="กฎหมาย ก")
        db.add(family_a); db.flush()
        doc_a = Document(knowledge_base_id=kb.id, original_filename="a.txt", stored_filename="a.txt",
                         storage_path="/tmp/a.txt", mime_type="text/plain", file_size=1,
                         checksum_sha256="fa" * 32, title="กฎหมาย ก ฉบับปรับปรุงล่าสุด", document_type="legal", status="completed")
        db.add(doc_a); db.flush()
        instrument_a = LegalInstrument(document_id=doc_a.id, knowledge_base_id=kb.id, family_id=family_a.id,
                                       official_title=doc_a.title, document_class="consolidated",
                                       version_role="latest_consolidated", status="in_force")
        db.add(instrument_a); db.flush()
        chunk_a = DocumentChunk(document_id=doc_a.id, knowledge_base_id=kb.id, chunk_index=1,
                                content="มาตรา ๑๐๔ ของกฎหมาย ก", content_sha256="fb" * 32,
                                char_start=0, char_end=10, token_count=3, section_kind="มาตรา", section_number="104")
        db.add(chunk_a); db.commit()

        # Family B: an unrelated law that happens to also have a มาตรา 104,
        # which WAS amended. Its amendment must never be attributed to Family A.
        family_b = LegalFamily(knowledge_base_id=kb.id, base_title="กฎหมาย ข", normalized_key="กฎหมาย ข")
        db.add(family_b); db.flush()
        doc_b = Document(knowledge_base_id=kb.id, original_filename="b.txt", stored_filename="b.txt",
                         storage_path="/tmp/b.txt", mime_type="text/plain", file_size=1,
                         checksum_sha256="fc" * 32, title="กฎหมาย ข ฉบับปรับปรุงล่าสุด", document_type="legal", status="completed")
        db.add(doc_b); db.flush()
        instrument_b = LegalInstrument(document_id=doc_b.id, knowledge_base_id=kb.id, family_id=family_b.id,
                                       official_title=doc_b.title, document_class="consolidated", status="in_force")
        db.add(instrument_b); db.flush()
        amend_b = _amending(db, kb, family_b, title="พระราชบัญญัติแก้ไขเพิ่มเติมกฎหมาย ข (ฉบับที่ 1) พ.ศ. 2560", when=date(2017, 1, 1))
        db.add(LegalInstrumentRelation(knowledge_base_id=kb.id, source_instrument_id=amend_b.id,
                                       target_instrument_id=instrument_b.id, relation="AMENDS",
                                       target_provision="104", review_status="verified"))
        db.commit()

        result = services._persist_legal_clause_result(
            db, query="มาตรา 104 ของกฎหมาย ก", kb_ids=[kb.id], token_id=None, intent="test",
            detail="test", rows=[(chunk_a, doc_a, instrument_a)], attribute_amendments=True,
        )
        assert "แก้ไขเพิ่มเติมโดย" not in result["answer"]


def test_resolve_legal_context_does_not_overwrite_a_correct_family_match_with_an_unrelated_latest_consolidated():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolve-cross-family-guard", name="Resolve cross family guard")
        db.add(kb); db.flush()

        # Family A: named directly in the query. Its sole consolidated
        # instrument is NOT tagged latest_consolidated, so it's matched via
        # the plain `candidates` fallback (services.py:105), not the
        # latest_consolidated tag.
        family_a = LegalFamily(knowledge_base_id=kb.id, base_title="กฎหมายเอ", normalized_key="กฎหมายเอ")
        db.add(family_a); db.flush()
        doc_a = Document(knowledge_base_id=kb.id, original_filename="a.txt", stored_filename="a.txt",
                         storage_path="/tmp/a.txt", mime_type="text/plain", file_size=1,
                         checksum_sha256="ga" * 32, title="กฎหมายเอ ฉบับรวม", document_type="legal", status="completed")
        db.add(doc_a); db.flush()
        instrument_a = LegalInstrument(document_id=doc_a.id, knowledge_base_id=kb.id, family_id=family_a.id,
                                       official_title=doc_a.title, document_class="consolidated",
                                       status="in_force", effective_from=date(2015, 1, 1))
        db.add(instrument_a)

        # Family B: a completely unrelated law with exactly one
        # latest_consolidated instrument, which must never win here.
        family_b = LegalFamily(knowledge_base_id=kb.id, base_title="กฎหมายบี", normalized_key="กฎหมายบี")
        db.add(family_b); db.flush()
        doc_b = Document(knowledge_base_id=kb.id, original_filename="b.txt", stored_filename="b.txt",
                         storage_path="/tmp/b.txt", mime_type="text/plain", file_size=1,
                         checksum_sha256="gb" * 32, title="กฎหมายบี ฉบับปรับปรุงล่าสุด", document_type="legal", status="completed")
        db.add(doc_b); db.flush()
        instrument_b = LegalInstrument(document_id=doc_b.id, knowledge_base_id=kb.id, family_id=family_b.id,
                                       official_title=doc_b.title, document_class="consolidated",
                                       version_role="latest_consolidated", status="in_force", effective_from=date(2020, 1, 1))
        db.add(instrument_b)
        db.commit()

        context = resolve_legal_context(db, "กฎหมายเอ มาตรา 5", [kb.id], _plan(), RetrievalPolicy())
        assert context.matched_instrument_ids == [instrument_a.id]
        assert instrument_b.id not in context.matched_instrument_ids
        assert context.preferred_document_ids == [doc_a.id]


def test_resolve_legal_context_ambiguity_candidates_exclude_the_non_tied_instrument():
    with SessionLocal() as db:
        kb = KnowledgeBase(code="resolve-ambiguity-candidates", name="Resolve ambiguity candidates")
        db.add(kb); db.flush()
        family = LegalFamily(knowledge_base_id=kb.id, base_title="กฎหมายซี", normalized_key="กฎหมายซี")
        db.add(family); db.flush()

        # One plain consolidated instrument that is NOT part of the tie.
        plain_doc = Document(knowledge_base_id=kb.id, original_filename="plain.txt", stored_filename="plain.txt",
                             storage_path="/tmp/plain.txt", mime_type="text/plain", file_size=1,
                             checksum_sha256="gc" * 32, title="กฎหมายซี ฉบับรวม", document_type="legal", status="completed")
        db.add(plain_doc); db.flush()
        plain_instrument = LegalInstrument(document_id=plain_doc.id, knowledge_base_id=kb.id, family_id=family.id,
                                           official_title=plain_doc.title, document_class="consolidated",
                                           status="in_force", effective_from=date(2010, 1, 1))
        db.add(plain_instrument)

        # Two instruments mistakenly both tagged latest_consolidated -- these
        # are the ones actually causing the ambiguity.
        tied_ids = []
        for index in (1, 2):
            doc = Document(knowledge_base_id=kb.id, original_filename=f"tied-{index}.txt", stored_filename=f"tied-{index}.txt",
                           storage_path=f"/tmp/tied-{index}.txt", mime_type="text/plain", file_size=1,
                           checksum_sha256=f"gd{index}" * 21, title=f"กฎหมายซี ฉบับปรับปรุงล่าสุด {index}",
                           document_type="legal", status="completed")
            db.add(doc); db.flush()
            instrument = LegalInstrument(document_id=doc.id, knowledge_base_id=kb.id, family_id=family.id,
                                        official_title=doc.title, document_class="consolidated",
                                        version_role="latest_consolidated", status="in_force",
                                        effective_from=date(2020 + index, 1, 1))
            db.add(instrument); db.flush()
            tied_ids.append(instrument.id)
        db.commit()

        context = resolve_legal_context(db, "กฎหมายซี มาตรา 5", [kb.id], _plan(), RetrievalPolicy())
        assert context.ambiguous_context is True
        assert sorted(context.candidate_instrument_ids) == sorted(tied_ids)
        assert plain_instrument.id not in context.candidate_instrument_ids


def test_provenance_treats_supersedes_as_terminal_like_repeals():
    with SessionLocal() as db:
        kb, family, current = _provenance_kb(db, "prov-supersedes")
        supersede = _amending(db, kb, family, title="พระราชบัญญัติแก้ไขเพิ่มเติมประมวลกฎหมายที่ดิน (ฉบับที่ 6) พ.ศ. 2535", when=date(1992, 1, 1))
        amend = _amending(db, kb, family, title="พระราชบัญญัติแก้ไขเพิ่มเติมประมวลกฎหมายที่ดิน (ฉบับที่ 9) พ.ศ. 2543", when=date(2000, 5, 1))
        db.add_all([
            LegalInstrumentRelation(knowledge_base_id=kb.id, source_instrument_id=supersede.id, target_instrument_id=current.id,
                                    relation="SUPERSEDES", target_provision="7", review_status="verified"),
            LegalInstrumentRelation(knowledge_base_id=kb.id, source_instrument_id=amend.id, target_instrument_id=current.id,
                                    relation="AMENDS", target_provision="7", review_status="verified"),
        ])
        db.commit()
        result = services.build_legal_provenance_result(db, "มาตรา 7 มีสถานะอย่างไร และถูกยกเลิกโดยกฎหมายใด", [kb.id])
        answer = result["answer"]
        assert "มาตรา 7 แทนที่โดย" in answer
        assert "(ฉบับที่ 6) พ.ศ. 2535" in answer
        # The cross-work AMENDS edge is flagged, not emitted as a peer statement.
        assert "หมายเหตุ" in answer and "คนละฉบับ" in answer
        assert "มาตรา 7 แก้ไขเพิ่มเติมโดย" not in answer


def test_build_default_current_legal_result_fails_closed_when_no_chunk_matches():
    with SessionLocal() as db:
        kb, family, current = _provenance_kb(db, "default-current-no-match")
        db.add(DocumentChunk(document_id=current.document_id, knowledge_base_id=kb.id, chunk_index=1,
                             content="เนื้อหาที่ไม่เกี่ยวข้องกับคำถามเลย", content_sha256="hb" * 32,
                             char_start=0, char_end=10, token_count=3))
        db.commit()
        result = services.build_default_current_legal_result(db, "หลักเกณฑ์ค่าธรรมเนียมปัจจุบัน", [kb.id])
        assert result["insufficient_evidence"] is True
        assert result["sources"] == []


def test_build_default_current_legal_result_uses_the_full_legal_concept_term_list():
    with SessionLocal() as db:
        kb, family, current = _provenance_kb(db, "default-current-full-terms")
        # "หวงห้าม" is in _LEGAL_CONCEPT_TERMS but was missing from the old,
        # smaller inline term list this builder used to hand-roll.
        db.add(DocumentChunk(document_id=current.document_id, knowledge_base_id=kb.id, chunk_index=1,
                             content="ที่ดินของรัฐที่ถูกหวงห้ามไว้เพื่อประโยชน์สาธารณะ", content_sha256="hc" * 32,
                             char_start=0, char_end=10, token_count=5))
        db.commit()
        result = services.build_default_current_legal_result(db, "ที่ดินของรัฐที่หวงห้ามมีหลักเกณฑ์อย่างไร", [kb.id])
        assert result["insufficient_evidence"] is False
        assert len(result["sources"]) == 1
