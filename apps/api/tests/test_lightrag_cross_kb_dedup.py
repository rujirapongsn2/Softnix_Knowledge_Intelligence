import json

import httpx

from app.retrieval import LightRAGRetrievalEngine


def test_lightrag_adapter_scopes_identical_content_per_knowledge_base():
    """The same text sent to two Knowledge Bases must not collide in LightRAG.

    LightRAG deduplicates by a global content hash. The adapter therefore
    prefixes the text with the Knowledge Base identity so the hash differs
    per Knowledge Base; re-ingesting the same text into the same Knowledge
    Base still hashes identically and is deduplicated there as before.
    """
    seen_texts: dict[str, str] = {}

    def handler(request: httpx.Request):
        if request.url.path == "/documents/text":
            payload = json.loads(request.content)
            seen_texts[payload["file_source"]] = payload["text"]
            return httpx.Response(202, json={"track_id": "track-x"})
        raise AssertionError(f"Unexpected request: {request.url.path}")

    engine = LightRAGRetrievalEngine(base_url="http://lightrag.test", client=httpx.Client(transport=httpx.MockTransport(handler)))
    body = "Shared contract body."
    engine.ingest("doc-1", "kb-1", body, "Contract")
    engine.ingest("doc-1", "kb-2", body, "Contract")

    text_one = seen_texts["skip/kb-1/softnix-kb=kb-1__doc=doc-1__Contract"]
    text_two = seen_texts["skip/kb-2/softnix-kb=kb-2__doc=doc-1__Contract"]
    assert text_one.startswith("[softnix-kb:kb-1]\n")
    assert text_two.startswith("[softnix-kb:kb-2]\n")
    assert text_one != text_two, "identical content in different KBs must hash differently"

    # Same Knowledge Base, same content: the hashed text is stable so
    # LightRAG's within-KB duplicate detection keeps working.
    engine.ingest("doc-2", "kb-1", body, "Contract copy")
    assert seen_texts["skip/kb-1/softnix-kb=kb-1__doc=doc-2__Contract copy"] == text_one
