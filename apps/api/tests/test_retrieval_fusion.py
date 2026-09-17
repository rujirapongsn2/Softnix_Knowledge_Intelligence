from app.retrieval import RetrievalEvidence
from app.services import apply_answer_reference_contract, compose_cited_answer, fuse_evidence, processing_retry_delay, sanitize_source_reference_urls


def source(document_id: str, relevance: float = 1.0) -> dict:
    return {"document_id": document_id, "title": document_id, "chunk_id": document_id, "excerpt": "evidence", "relevance": relevance}


def test_reciprocal_rank_fusion_boosts_cross_channel_evidence():
    semantic = RetrievalEvidence([source("doc-a"), source("doc-b")], [], [], [])
    full_text = RetrievalEvidence([source("doc-b"), source("doc-a")], [], [], [])
    graph = RetrievalEvidence([source("doc-a")], [], [], [])
    result = fuse_evidence(semantic, full_text, graph, limit=2)
    assert [item["document_id"] for item in result.sources] == ["doc-a", "doc-b"]
    assert [item["citation_id"] for item in result.sources] == ["S1", "S2"]


def test_reciprocal_rank_fusion_keeps_distinct_chunks_from_one_document():
    first = {**source("doc-a"), "chunk_id": "chunk-1", "excerpt": "General introduction."}
    second = {**source("doc-a"), "chunk_id": "chunk-2", "excerpt": "DOL Smart Survey evidence."}
    result = fuse_evidence(RetrievalEvidence([first, second], [], [], []), limit=2)
    assert [item["chunk_id"] for item in result.sources] == ["chunk-1", "chunk-2"]


def test_cited_answer_preserves_engine_response_and_adds_platform_citations():
    evidence = RetrievalEvidence(
        [{**source("doc-a"), "citation_id": "S1"}], [], [], [],
        "DOL Smart Survey is supported by Softnix Technology.",
    )
    answer = compose_cited_answer(evidence)
    assert answer.startswith("DOL Smart Survey is supported")
    # The engine answer had no [S#]; a citation line plus the citation-detail
    # block are appended so every claim stays traceable.
    assert "แหล่งอ้างอิง: [S1]" in answer
    assert answer.rstrip().endswith("[S1] doc-a")


def test_cited_answer_does_not_duplicate_existing_platform_citations():
    evidence = RetrievalEvidence(
        [{**source("doc-a"), "citation_id": "S1"}], [], [], [],
        "The service runs on APP-01 [S1].",
    )
    answer = compose_cited_answer(evidence)
    # The answer already cites [S1]; no second inline citation line is added,
    # only the trailing citation-detail block.
    assert answer.startswith("The service runs on APP-01 [S1].")
    assert "แหล่งอ้างอิง: [S1]" not in answer
    assert "รายละเอียดแหล่งอ้างอิง:" in answer


def test_processing_retry_delay_is_bounded_exponential_backoff():
    assert [processing_retry_delay(attempt) for attempt in (1, 2, 3, 10)] == [2, 4, 8, 60]


def test_duplicate_hits_cannot_outvote_independent_channels():
    repeated = RetrievalEvidence([source("noise")] * 20 + [source("relevant")], [], [], [])
    independent = RetrievalEvidence([source("relevant")], [], [], [])
    result = fuse_evidence(repeated, independent, limit=2)
    assert [s["document_id"] for s in result.sources] == ["relevant", "noise"]


def test_partial_invalid_rerank_preserves_distinct_evidence(monkeypatch):
    from app import services

    class Reranker:
        reranker_enabled = True

        def rerank(self, *args):
            return [(1, 0.9), (1, 0.8), (0, float("nan")), (99, 1.0)]

    monkeypatch.setattr(services, "OpenRouterClient", Reranker)
    evidence = RetrievalEvidence([source("law"), source("research"), source("sop")], [], [], [])
    result = services.rerank_evidence("requirements", evidence, 3)
    assert [s["document_id"] for s in result.sources] == ["research", "law", "sop"]
    assert [s["citation_id"] for s in result.sources] == ["S1", "S2", "S3"]


def test_empty_explicit_metadata_scope_skips_external_retrieval(monkeypatch):
    from app import services
    from app.planner import RetrievalPlan

    def unexpected_engine():
        raise AssertionError("Empty metadata scope must not call retrieval")

    monkeypatch.setattr(services, "LightRAGRetrievalEngine", unexpected_engine)
    trace = []
    result = services.query_documents(None, "SOP", ["kb"], 3, trace,
                                      RetrievalPlan(intent="search", metadata_document_ids=[]))
    assert result.sources == []
    assert "retrieval skipped" in trace[-1]["detail"]


def test_parallel_retrieval_inherits_request_deadline(monkeypatch):
    from app import services
    from app.planner import RetrievalChannel, RetrievalPlan
    from app.request_budget import remaining_timeout, reset_deadline, set_deadline

    observed = []

    def run_channel(callback, needs_db):
        observed.append(remaining_timeout(120))
        return RetrievalEvidence([], [], [], [])

    monkeypatch.setattr(services, "_run_retrieval_channel", run_channel)
    monkeypatch.setattr(services, "_legal_instruments_by_document", lambda *args: {})
    token = set_deadline(10)
    try:
        services.query_documents(None, "research", ["kb"], 3,
                                 plan=RetrievalPlan(intent="search", channels=[RetrievalChannel.FULLTEXT]))
    finally:
        reset_deadline(token)
    assert len(observed) == 1
    assert 0 < observed[0] <= 10


def test_cited_answer_prefers_ingested_pdf_url_over_source_uri():
    evidence = RetrievalEvidence(
        [{
            "citation_id": "S1",
            "document_id": "doc-pdf",
            "title": "พ.ร.บ. ตัวอย่าง",
            "section_label": "มาตรา 1",
            "excerpt": "ข้อความ",
            "source_uri": "https://searchlaw.ocs.go.th/example",
            "mime_type": "application/pdf",
            "original_filename": "sample.pdf",
            "download_url": "https://knowledge.softnix.ai/api/v1/documents/doc-pdf/file",
        }],
        [], [], [],
        "คำตอบจากหลักฐาน [S1]",
    )
    answer = compose_cited_answer(evidence)
    assert "https://knowledge.softnix.ai/api/v1/documents/doc-pdf/file" in answer
    assert "searchlaw.ocs.go.th" not in answer


def test_cited_answer_prefers_ski_download_url_for_text_plain():
    evidence = RetrievalEvidence(
        [{
            "citation_id": "S1",
            "document_id": "doc-txt",
            "title": "เอกสารข้อความ",
            "excerpt": "ข้อความ",
            "source_uri": "https://searchlaw.ocs.go.th/example",
            "mime_type": "text/plain",
            "original_filename": "note.txt",
            "download_url": "https://knowledge.softnix.ai/api/v1/documents/doc-txt/file",
        }],
        [], [], [],
        "คำตอบ [S1]",
    )
    answer = compose_cited_answer(evidence)
    assert "https://knowledge.softnix.ai/api/v1/documents/doc-txt/file" in answer
    assert "searchlaw.ocs.go.th" not in answer
    assert "ocs.go.th" not in answer


def test_cited_answer_omits_ocs_only_source_uri():
    evidence = RetrievalEvidence(
        [{
            "citation_id": "S1",
            "document_id": "doc-ocs",
            "title": "เอกสาร OCS เท่านั้น",
            "excerpt": "ข้อความ",
            "source_uri": "https://searchlaw.ocs.go.th/council-of-state/#/public/doc/abc",
            "mime_type": "text/plain",
            "original_filename": "note.txt",
            "download_url": None,
        }],
        [], [], [],
        "คำตอบ [S1]",
    )
    answer = compose_cited_answer(evidence)
    assert "searchlaw.ocs.go.th" not in answer
    assert "ocs.go.th" not in answer
    assert "council-of-state" not in answer
    assert "[S1] เอกสาร OCS เท่านั้น" in answer


def test_structured_sources_omit_ocs_urls_including_nested_provenance():
    original = [{
        "citation_id": "S1",
        "source_uri": "https://searchlaw.ocs.go.th/example",
        "provenance": {
            "origin": "legal_registry",
            "source_uri": "https://www.ocs.go.th/example",
        },
    }]
    sanitized = sanitize_source_reference_urls(original)
    assert sanitized[0]["source_uri"] is None
    assert sanitized[0]["provenance"]["source_uri"] is None
    # Response sanitization must not mutate an ORM-backed cached payload.
    assert original[0]["source_uri"].startswith("https://")
    assert original[0]["provenance"]["source_uri"].startswith("https://")


def test_answer_reference_contract_is_frontend_ready_and_versioned():
    result = apply_answer_reference_contract({
        "answer": "ระบบใช้ข้อกำหนดนี้ [S1]\n\nรายละเอียดแหล่งอ้างอิง:\n[S1] เอกสาร",
        "sources": [{
            "citation_id": "S1",
            "document_id": "doc-1",
            "chunk_id": "chunk-1",
            "title": "ข้อกำหนดตัวอย่าง",
            "section_label": "ข้อ 4",
            "excerpt": "หลักฐาน",
            "original_filename": "rule.pdf",
            "mime_type": "application/pdf",
            "download_url": "https://knowledge.softnix.ai/api/v1/documents/doc-1/file",
        }],
    })
    assert result["schema_version"] == "ski.answer.v1"
    assert result["claims"] == [{
        "id": "C1",
        "text": "ระบบใช้ข้อกำหนดนี้",
        "citation_ids": ["S1"],
        "reference_ids": ["R1"],
    }]
    reference = result["references"][0]
    assert reference["id"] == "R1"
    assert reference["locator"]["section"] == "ข้อ 4"
    assert reference["locator"]["page_start"] is None
    assert reference["file"]["available"] is True
    assert reference["file"]["download_url"].endswith("/doc-1/file")
    assert reference["file"]["access"]["authentication"] == "bearer_or_session"
