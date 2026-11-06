import json

import httpx
import pytest

from app.config import get_settings
from app.grounded_answer import INSUFFICIENT_ANSWER, render_grounded_answer
from app.llm import LLMClient
from app.passages import context_budgets, named_document_ids, passage_query, rank_passages, select_passage


def test_short_clause_keeps_its_exception_after_character_500():
    clause = "ผู้ควบคุมต้องทบทวนมาตรการ " + "ปัจจัยแวดล้อม " * 45 + "เว้นแต่เหตุละเมิดไม่มีความเสี่ยง"
    assert len(clause) > 500
    assert select_passage("ต้องทบทวนเมื่อใด มีข้อยกเว้นหรือไม่", clause) == clause


def test_context_budget_does_not_drop_later_sources():
    budgets = context_budgets([6000] * 9 + [794])
    assert len(budgets) == 10 and all(b > 0 for b in budgets)
    assert budgets[-1] == 794 and sum(budgets) <= 36000


def test_long_text_selects_requested_rule_from_tail_instead_of_prefix():
    content = ("การจัดประชุมและรายงานผลประจำปี\n\n" * 200
               + "มาตรการต้องทบทวนเมื่อเทคโนโลยีเปลี่ยนแปลง เว้นแต่เหตุละเมิดไม่มีความเสี่ยง\n\n")
    excerpt = select_passage("ทบทวนมาตรการเมื่อเทคโนโลยีเปลี่ยนแปลง มีข้อยกเว้นเหตุละเมิดหรือไม่", content, 2000)
    assert "เว้นแต่เหตุละเมิดไม่มีความเสี่ยง" in excerpt
    assert len(excerpt) <= 2000


def test_title_is_not_the_passage_ranking_query():
    title = "ข้อกำหนดการจัดสรรที่ดินเพื่อเกษตรกรรมจังหวัดชัยนาท พ.ศ. 2550"
    query = passage_query(f"ตาม{title} ที่ดินแปลงย่อยมีเนื้อที่ขั้นต่ำเท่าใด", title)
    texts = [title + " บทนำ", "ที่ดินแปลงย่อยต้องมีเนื้อที่ไม่ต่ำกว่า 2 ไร่"]
    assert rank_passages(query, texts)[0] == 1


def test_explicit_province_and_instrument_year_select_exact_version():
    identities = [
        ("wrong-province", "ข้อกำหนดเกี่ยวกับการจัดสรรที่ดินจังหวัดระนอง (ฉบับที่ 3) พ.ศ. 2558", ""),
        ("wrong-version", "ข้อกำหนดเกี่ยวกับการจัดสรรที่ดินจังหวัดพังงา (ฉบับที่ 4) พ.ศ. 2560", ""),
        ("right", "ข้อกำหนด_เกี่ยวกับการจัดสรรที่ดินจังหวัดพังงา-(ฉบับที่-๓)_๒๑-มกราคม-๒๕๕๙.md",
         "ข้อกำหนดเกี่ยวกับการจัดสรรที่ดินจังหวัดพังงา (ฉบับที่ ๓) พ.ศ. ๒๕๕๘"),
    ]
    query = "ข้อกำหนดเกี่ยวกับการจัดสรรที่ดินจังหวัดพังงา ฉบับที่ 3 พ.ศ. 2558 ต้องมีถังน้ำเท่าไร"
    assert named_document_ids(query, identities) == ["right"]
    assert named_document_ids(query.replace("2558", "2561"), identities) is None
    assert named_document_ids(query + " เปรียบเทียบกับจังหวัดระนอง", identities) is None
    assert named_document_ids(query + " เปรียบเทียบกับฉบับที่ 4", identities) is None
    assert named_document_ids(query + " และกฎหมาย พ.ศ. 2543", identities) is None


def test_structured_claims_render_only_their_declared_supplied_ids():
    content = json.dumps({"claims": [{"text": "ต้องแจ้งภายใน 72 ชั่วโมง", "source_ids": ["S3", "S3"]}],
                          "insufficient_evidence": False}, ensure_ascii=False)
    assert render_grounded_answer(content, {"S3"}) == "ต้องแจ้งภายใน 72 ชั่วโมง [S3]"


def test_answer_schema_example_does_not_invent_s1_when_it_was_filtered_out(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    get_settings.cache_clear()

    def handler(request):
        system = json.loads(request.content)["messages"][0]["content"]
        assert '"source_ids": ["S3"]' in system
        assert '"source_ids": ["S1"]' not in system
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "claims": [{"text": "Fact", "source_ids": ["S3"]}], "insufficient_evidence": False})}}]})

    try:
        client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
        assert client.answer_from_sources("Question", [{"citation_id": "S3", "title": "Doc", "excerpt": "Fact"}]) == "Fact [S3]"
    finally:
        get_settings.cache_clear()


def test_compatible_server_cited_prose_is_valid_even_when_it_starts_with_a_citation():
    assert render_grounded_answer("[S1] Supported fact", {"S1"}) == "[S1] Supported fact"
    with pytest.raises(RuntimeError):
        render_grounded_answer("Uncited fact", {"S1"})


@pytest.mark.parametrize("claim", [
    {"text": "Unsupported", "source_ids": ["S99"]},
    {"text": "Unsupported [S99]", "source_ids": ["S1"]},
    {"text": "Undeclared [S2]", "source_ids": ["S1"]},
    {"text": "Uncited", "source_ids": []},
])
def test_invalid_claim_ids_are_not_repaired_by_guessing(claim):
    with pytest.raises(RuntimeError):
        render_grounded_answer(json.dumps({"claims": [claim], "insufficient_evidence": False}), {"S1", "S2"})


def test_structured_abstention_is_distinct_from_missing_citations():
    value = render_grounded_answer('{"claims": [], "insufficient_evidence": true}', {"S1"})
    assert value == INSUFFICIENT_ANSWER
    from app.retrieval import RetrievalEvidence
    from app.services import _validate_answer_citations
    evidence = RetrievalEvidence([{"citation_id": "S1"}], [], [], [], value)
    warnings = []
    _validate_answer_citations(evidence, warnings)
    assert evidence.answer == INSUFFICIENT_ANSWER
    assert warnings == []


def test_answer_prompt_keeps_tail_qualifiers_and_requests_structured_citations(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    get_settings.cache_clear()
    excerpt = "บริบท " * 700 + "ข้อยกเว้นไม่มีความเสี่ยงต่อสิทธิและเสรีภาพ"

    def handler(request):
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert "ข้อยกเว้นไม่มีความเสี่ยงต่อสิทธิและเสรีภาพ" in payload["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "claims": [{"text": "ยกเว้นเมื่อไม่มีความเสี่ยง", "source_ids": ["S1"]}],
            "insufficient_evidence": False,
        })}}]})

    try:
        client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
        assert client.answer_from_sources("มีข้อยกเว้นใด", [{"citation_id": "S1", "title": "ประกาศ", "excerpt": excerpt}]).endswith("[S1]")
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("always_invalid", [False, True])
def test_invalid_citation_regeneration_is_bounded_and_still_validates_ids(monkeypatch, always_invalid):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    get_settings.cache_clear()
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        invalid = always_invalid or len(seen) == 1
        content = json.dumps({"claims": [{"text": "Fact", "source_ids": ["S99" if invalid else "S1"]}],
                              "insufficient_evidence": False})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    try:
        client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
        if always_invalid:
            with pytest.raises(RuntimeError, match="CITATIONS_INVALID"):
                client.answer_from_sources("Question", [{"citation_id": "S1", "title": "Doc", "excerpt": "Fact"}])
        else:
            assert client.answer_from_sources("Question", [{"citation_id": "S1", "title": "Doc", "excerpt": "Fact"}]) == "Fact [S1]"
        assert len(seen) == client.answer_attempts == 2
        assert "S99" not in json.dumps(seen[-1]), "Invalid claims must not become retry evidence"
    finally:
        get_settings.cache_clear()


def test_answer_can_cite_last_source_when_all_ten_passages_are_long(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    get_settings.cache_clear()

    def handler(request):
        prompt = json.loads(request.content)["messages"][1]["content"]
        assert all(f"[S{i}]" in prompt for i in range(1, 11))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "claims": [{"text": "Fact from last source", "source_ids": ["S10"]}], "insufficient_evidence": False})}}]})

    try:
        client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
        sources = [{"citation_id": f"S{i}", "title": "Doc", "excerpt": "Long passage " * 500} for i in range(1, 11)]
        assert client.answer_from_sources("Question", sources).endswith("[S10]")
    finally:
        get_settings.cache_clear()


def test_citation_regeneration_does_not_send_another_request_after_deadline(monkeypatch):
    import app.llm as llm
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    get_settings.cache_clear()
    requests = []
    budgets = []

    def remaining(default):
        budgets.append(default)
        if len(budgets) > 1:
            raise RuntimeError("MCP_TIMEOUT")
        return 1

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"claims":[{"text":"Fact","source_ids":["S99"]}],"insufficient_evidence":false}'}}]})

    monkeypatch.setattr(llm, "remaining_timeout", remaining)
    try:
        client = LLMClient(httpx.Client(transport=httpx.MockTransport(handler)))
        with pytest.raises(RuntimeError, match="MCP_TIMEOUT"):
            client.answer_from_sources("Question", [{"citation_id": "S1", "title": "Doc", "excerpt": "Fact"}])
        assert len(requests) == 1
    finally:
        get_settings.cache_clear()


def test_candidate_pool_is_larger_than_final_sources_even_when_reranker_is_off(monkeypatch):
    from app import services
    from app.planner import RetrievalChannel, RetrievalPlan
    from app.retrieval import RetrievalEvidence

    observed = []

    def chunks(db, query, ids, limit, *args):
        observed.append(limit)
        return RetrievalEvidence([{"document_id": f"d{i}", "chunk_id": str(i), "excerpt": "fact", "title": "Doc"}
                                  for i in range(limit)], [], [], [])

    monkeypatch.setattr(services, "query_database_chunks", chunks)
    monkeypatch.setattr(services, "_run_retrieval_channel", lambda cb, needs_db: cb(None))
    monkeypatch.setattr(services, "_legal_instruments_by_document", lambda *args: {})
    monkeypatch.setattr(services, "decorate_sources", lambda *args: None)
    plan = RetrievalPlan(intent="search", channels=[RetrievalChannel.FULLTEXT], rerank_enabled=False)
    result = services.query_documents(None, "a question", ["kb"], 3, plan=plan, generate_answer=False)
    assert observed[0] >= get_settings().rerank_candidate_limit
    assert len(result.sources) == 3
    assert [s["citation_id"] for s in result.sources] == ["S1", "S2", "S3"]


def test_named_passages_keep_metadata_date_and_kb_constraints():
    from datetime import date
    from sqlalchemy.orm import Session
    from app.db import Base, engine_for
    from app.models import Document, DocumentChunk, KnowledgeBase
    from app.planner import RetrievalPlan
    from app.services import query_database_chunks

    engine = engine_for("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            kb = KnowledgeBase(code="scope", name="Scope")
            other = KnowledgeBase(code="other", name="Other")
            db.add_all([kb, other]); db.flush()
            documents = []
            for index, owner in enumerate((kb, kb, other)):
                document = Document(knowledge_base_id=owner.id, title=f"Regulation {index}",
                                    original_filename=f"{index}.txt", stored_filename=f"{index}.txt", storage_path=f"/{index}",
                                    mime_type="text/plain", file_size=800, checksum_sha256=str(index), status="completed",
                                    published_at=date(2020, 1, 1))
                db.add(document); db.flush(); documents.append(document)
                content = "บทนำ " * 100 + "เว้นแต่ไม่มีความเสี่ยงต่อสิทธิและเสรีภาพ"
                db.add(DocumentChunk(document_id=document.id, knowledge_base_id=owner.id, chunk_index=0,
                                     content=content, content_sha256=str(index), char_start=0, char_end=len(content), token_count=100))
            db.flush()
            plan = RetrievalPlan(intent="search", named_document_ids=[d.id for d in documents])
            result = query_database_chunks(db, "ข้อยกเว้นความเสี่ยง", [kb.id], 5, plan=plan)
            assert {s["document_id"] for s in result.sources} == {d.id for d in documents[:2]}
            assert all(s["excerpt"].endswith("เว้นแต่ไม่มีความเสี่ยงต่อสิทธิและเสรีภาพ") for s in result.sources)
            plan = plan.model_copy(update={"named_document_ids": [documents[0].id], "metadata_document_ids": [documents[1].id]})
            assert query_database_chunks(db, "ข้อยกเว้น", [kb.id], 5, plan=plan).sources == []
            plan = plan.model_copy(update={"metadata_document_ids": None, "published_from": date(2021, 1, 1)})
            assert query_database_chunks(db, "ข้อยกเว้น", [kb.id], 5, plan=plan).sources == []
    finally:
        engine.dispose()


def test_text_after_a_paragraph_cut_stays_selectable():
    filler = "บทนำทั่วไปของประกาศ "
    clause = "เว้นแต่ได้รับอนุญาตเป็นหนังสือจากอธิบดี"
    head = "ก" * 1000 + "\n\n" + filler * 10
    content = head + clause + filler * (4452 // len(filler))
    assert 1002 < content.index(clause) < 1400
    assert clause in select_passage("ได้รับอนุญาตเป็นหนังสือจากอธิบดี", content, 2000)


def test_rerank_candidate_limit_from_an_older_env_is_clamped_not_rejected(monkeypatch):
    monkeypatch.setenv("RERANK_CANDIDATE_LIMIT", "150")
    get_settings.cache_clear()
    try:
        assert get_settings().rerank_candidate_limit == 100
    finally:
        get_settings.cache_clear()


def test_abstention_does_not_present_retrieved_sources_as_its_citations():
    from app.retrieval import RetrievalEvidence
    from app.services import apply_answer_reference_contract, compose_cited_answer
    sources = [{"citation_id": "S1", "title": "Doc 1", "excerpt": "a"}, {"citation_id": "S2", "title": "Doc 2", "excerpt": "b"}]
    answer = compose_cited_answer(RetrievalEvidence(sources, [], [], [], INSUFFICIENT_ANSWER))
    assert answer.startswith(INSUFFICIENT_ANSWER)
    assert "แหล่งอ้างอิง: [S1] [S2]" not in answer
    assert "[S1] Doc 1" in answer
    assert apply_answer_reference_contract({"answer": answer, "sources": sources})["claims"] == []
