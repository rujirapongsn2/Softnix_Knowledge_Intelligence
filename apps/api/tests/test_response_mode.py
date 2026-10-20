"""response_mode "evidence" returns the sources without paying for an LLM answer."""
import uuid

from fastapi.testclient import TestClient

from app import services
from app.main import app


class _CountingLLM:
    answers = 0
    reranker_enabled = False
    llm_enabled = True

    def __init__(self, *_args):
        pass

    def answer_from_sources(self, query, sources):
        type(self).answers += 1
        return "Customer Portal runs on APP-01 [S1]."

    def plan_retrieval(self, *_args):
        raise RuntimeError("LLM_PLANNER_UNAVAILABLE")


def test_evidence_mode_skips_the_llm_answer_and_the_other_modes_keep_it(monkeypatch):
    monkeypatch.setattr(services, "LLMClient", _CountingLLM)
    _CountingLLM.answers = 0
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).status_code == 200
        kb = client.post("/api/v1/knowledge-bases", json={"name": "Modes", "code": f"modes-{uuid.uuid4().hex[:8]}"}).json()
        client.post(f"/api/v1/knowledge-bases/{kb['id']}/activate")
        client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("portal.txt", b"Customer Portal runs on APP-01.", "text/plain")})
        while client.post("/api/v1/internal/process-next").json()["processed"]:
            pass

        def ask(mode):
            reply = client.post("/api/v1/query", json={"query": "What runs on APP-01?", "knowledge_base_ids": [kb["id"]], "response_mode": mode})
            assert reply.status_code == 200
            return reply.json()

        evidence = ask("evidence")
        assert evidence["sources"] and _CountingLLM.answers == 0
        trace = evidence["metadata"]["retrieval_trace"]
        assert any(step["channel"] == "answer_generation" and step["status"] == "skipped" for step in trace)
        ask("both")
        ask("answer")
        assert _CountingLLM.answers == 2
