"""Engine-neutral retrieval contracts; LightRAG REST details are isolated here."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .config import get_settings
from .content_safety import protect_document_text, protect_query_text
from .request_budget import remaining_timeout


@dataclass
class RetrievalEvidence:
    sources: list[dict]
    entities: list[dict]
    relationships: list[dict]
    paths: list[dict]
    answer: str | None = None


class RetrievalEngine(ABC):
    @abstractmethod
    def ingest_document(self, document_id: str, text: str) -> None: ...

    @abstractmethod
    def delete_document(self, document_id: str) -> None: ...

    @abstractmethod
    def query(self, query: str, knowledge_base_ids: list[str], limit: int) -> RetrievalEvidence: ...


class LightRAGRetrievalEngine(RetrievalEngine):
    """LightRAG API server adapter.

    This intentionally depends only on the stable REST boundary, not the LightRAG
    Python SDK. A knowledge-base ID becomes an isolated LightRAG workspace.
    """
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        reranker_enabled: bool | None = None,
    ):
        settings = get_settings()
        self.base_url = (base_url if base_url is not None else settings.lightrag_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.lightrag_api_key
        self.timeout = settings.lightrag_timeout_seconds
        self.workspace_prefix = settings.lightrag_workspace_prefix
        # Platform reranking is performed after fusion. Do not enable the
        # LightRAG server's separate reranker unless it is configured there.
        self.reranker_enabled = False if reranker_enabled is None else reranker_enabled
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def _workspace(self, knowledge_base_id: str) -> str:
        return f"{self.workspace_prefix}{knowledge_base_id}"

    @staticmethod
    def _source_label(knowledge_base_id: str, document_id: str, title: str) -> str:
        """Return a source basename that survives LightRAG path normalisation."""
        return f"softnix-kb={knowledge_base_id}__doc={document_id}__{title}"

    @staticmethod
    def _decode_source_label(file_path: str) -> tuple[str, str, str] | None:
        parts = file_path.rsplit("/", 1)[-1].split("__", 2)
        if len(parts) != 3 or not parts[0].startswith("softnix-kb=") or not parts[1].startswith("doc="):
            return None
        return parts[0].removeprefix("softnix-kb="), parts[1].removeprefix("doc="), parts[2]

    def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("RETRIEVAL_ENGINE_UNAVAILABLE")
        client = self._client or httpx.Client(timeout=remaining_timeout(self.timeout))
        try:
            response = client.request(method, f"{self.base_url}{path}", headers=self._headers(), json=json)
            response.raise_for_status()
            return response.json() if response.content else {}
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                raise RuntimeError("RETRIEVAL_ENGINE_BUSY") from exc
            raise RuntimeError("RETRIEVAL_ENGINE_REJECTED") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("RETRIEVAL_ENGINE_UNAVAILABLE") from exc
        finally:
            if self._client is None:
                client.close()

    def ingest(self, document_id: str, knowledge_base_id: str, text: str, title: str) -> str | None:
        # LightRAG Server v1.5's InsertTextRequest accepts only text,
        # file_source, and optional chunking. LightRAG reduces this to its
        # basename in references, so keep platform identifiers in that basename.
        source_label = self._source_label(knowledge_base_id, document_id, title)
        payload = {"text": protect_document_text(text), "file_source": f"skip/{knowledge_base_id}/{source_label}"}
        data = self._request("POST", "/documents/text", json=payload)
        return data.get("track_id") or data.get("id")

    def ingest_document(self, document_id: str, text: str) -> None:
        self.ingest(document_id, "default", text, document_id)

    def track_status(self, track_id: str) -> str:
        """Return pending, processed, or failed for a LightRAG ingestion track."""
        data = self._request("GET", f"/documents/track_status/{quote(track_id, safe='')}")
        statuses = [item.get("status", "").upper() for item in data.get("documents", [])]
        if any(status in {"FAILED", "ERROR"} for status in statuses):
            return "failed"
        if statuses and all(status == "PROCESSED" for status in statuses):
            return "processed"
        return "pending"

    def delete_document(self, document_id: str) -> None:
        # Deletion behavior is version-specific; use the document API once a
        # pinned LightRAG server version exposes its deletion contract.
        raise RuntimeError("RETRIEVAL_DELETE_NOT_CONFIGURED")

    def graph_labels(self) -> list[str]:
        """Return LightRAG node labels for a best-effort local graph sync."""
        data = self._request("GET", "/graph/label/list")
        return [item for item in data if isinstance(item, str)] if isinstance(data, list) else []

    def graph(self, label: str, max_depth: int = 1, max_nodes: int = 100) -> dict:
        return self._request("GET", f"/graphs?label={quote(label, safe='')}&max_depth={max_depth}&max_nodes={max_nodes}")

    def query(self, query: str, knowledge_base_ids: list[str], limit: int) -> RetrievalEvidence:
        if len(knowledge_base_ids) != 1:
            raise RuntimeError("MULTI_KNOWLEDGE_BASE_ENGINE_QUERY_UNSUPPORTED")
        data = self._request("POST", "/query", json={
            "query": protect_query_text(query),
            "mode": "mix",
            "include_references": True,
            "include_chunk_content": True,
            "enable_rerank": self.reranker_enabled,
            "top_k": limit,
        })
        sources: list[dict] = []
        requested_knowledge_base_id = knowledge_base_ids[0]
        for index, reference in enumerate(data.get("references", [])[:limit], 1):
            file_path = reference.get("file_path", "")
            source_identity = self._decode_source_label(file_path)
            # The configured development LightRAG server shares one index and
            # returns only the file_source basename in references. Release only
            # evidence carrying the selected Knowledge Base's identity.
            if source_identity is None or source_identity[0] != requested_knowledge_base_id:
                continue
            _, source_document_id, source_title = source_identity
            content = reference.get("content", [])
            if isinstance(content, list):
                content = "\n\n".join(content)
            sources.append({
                "citation_id": f"S{index}",
                "document_id": reference.get("metadata", {}).get("document_id", source_document_id),
                "title": source_title or reference.get("file_name") or "LightRAG source",
                "chunk_id": str(reference.get("reference_id", index)),
                "excerpt": content or "",
                "relevance": reference.get("score", 0.0),
            })
        # The current development LightRAG deployment has a shared index. Its
        # generated response may synthesize content from references outside the
        # selected KB, so only return identity-checked evidence. The platform
        # generates the final answer from these scoped excerpts.
        return RetrievalEvidence(sources, [], [], [], None)
