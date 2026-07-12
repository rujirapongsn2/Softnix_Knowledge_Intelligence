from app.retrieval import RetrievalEvidence
from app.services import compose_cited_answer, fuse_evidence, processing_retry_delay


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
    assert answer.endswith("Sources: [S1]")


def test_cited_answer_does_not_duplicate_existing_platform_citations():
    evidence = RetrievalEvidence(
        [{**source("doc-a"), "citation_id": "S1"}], [], [], [],
        "The service runs on APP-01 [S1].",
    )
    assert compose_cited_answer(evidence) == "The service runs on APP-01 [S1]."


def test_processing_retry_delay_is_bounded_exponential_backoff():
    assert [processing_retry_delay(attempt) for attempt in (1, 2, 3, 10)] == [2, 4, 8, 60]
