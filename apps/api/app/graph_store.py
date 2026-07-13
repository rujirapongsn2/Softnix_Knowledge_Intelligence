"""Neo4j projection adapter; PostgreSQL remains the platform source of truth."""
import logging
from typing import Any

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)


class Neo4jGraphStore:
    def __init__(self, base_url: str | None = None, username: str | None = None, password: str | None = None,
                 database: str | None = None, client: httpx.Client | None = None):
        settings = get_settings()
        self.base_url = (base_url if base_url is not None else settings.neo4j_http_url).rstrip("/")
        self.username = username if username is not None else settings.neo4j_username
        self.password = password if password is not None else settings.neo4j_password
        self.database = database if database is not None else settings.neo4j_database
        self.timeout = settings.neo4j_timeout_seconds
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.password)

    def _execute(self, statement: str, parameters: dict[str, Any]) -> None:
        if not self.enabled:
            return
        client = self._client or httpx.Client(timeout=self.timeout)
        try:
            response = client.post(
                f"{self.base_url}/db/{self.database}/tx/commit",
                auth=(self.username, self.password),
                json={"statements": [{"statement": statement, "parameters": parameters}]},
            )
            response.raise_for_status()
            errors = response.json().get("errors", [])
            if errors:
                raise RuntimeError(errors[0].get("message", "Neo4j transaction failed"))
        finally:
            if self._client is None:
                client.close()

    def _query(self, statement: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.enabled:
            raise RuntimeError("NEO4J_NOT_CONFIGURED")
        client = self._client or httpx.Client(timeout=self.timeout)
        try:
            response = client.post(
                f"{self.base_url}/db/{self.database}/tx/commit",
                auth=(self.username, self.password),
                json={"statements": [{"statement": statement, "parameters": parameters}]},
            )
            response.raise_for_status()
            payload = response.json()
            errors = payload.get("errors", [])
            if errors:
                raise RuntimeError(errors[0].get("message", "Neo4j query failed"))
            return payload.get("results", [])
        finally:
            if self._client is None:
                client.close()

    def upsert_entity(self, entity) -> None:
        self._execute(
            "MERGE (node:KnowledgeEntity {id: $id}) "
            "SET node.knowledge_base_id = $knowledge_base_id, node.name = $name, "
            "node.canonical_name = $canonical_name, node.entity_type = $entity_type, "
            "node.description = $description, node.origin = $origin, node.review_status = $review_status, "
            "node.is_legal = $is_legal, node.updated_at = datetime()",
            {"id": entity.id, "knowledge_base_id": entity.knowledge_base_id, "name": entity.name,
             "canonical_name": entity.canonical_name, "entity_type": entity.entity_type,
             "description": entity.description, "origin": getattr(entity, "origin", "manual"),
             "review_status": getattr(entity, "review_status", "verified"),
             "is_legal": getattr(entity, "is_legal", False)},
        )

    def check(self) -> bool:
        if not self.enabled:
            return False
        self._execute("RETURN 1", {})
        return True

    def upsert_relationship(self, relationship, source, target) -> None:
        self._execute(
            "MATCH (source:KnowledgeEntity {id: $source_id}), (target:KnowledgeEntity {id: $target_id}) "
            "MERGE (source)-[edge:KNOWLEDGE_RELATIONSHIP {id: $id}]->(target) "
            "SET edge.knowledge_base_id = $knowledge_base_id, edge.relationship_type = $relationship_type, "
            "edge.description = $description, edge.confidence = $confidence, edge.origin = $origin, "
            "edge.review_status = $review_status, edge.is_legal = $is_legal, edge.updated_at = datetime()",
            {"id": relationship.id, "source_id": source.id, "target_id": target.id,
             "knowledge_base_id": relationship.knowledge_base_id, "relationship_type": relationship.relationship_type,
             "description": relationship.description, "confidence": relationship.confidence,
             "origin": getattr(relationship, "origin", "manual"),
             "review_status": getattr(relationship, "review_status", "verified"),
             "is_legal": getattr(relationship, "is_legal", False)},
        )

    def traverse(self, entity_id: str, knowledge_base_id: str, depth: int = 1, max_nodes: int = 100) -> dict[str, list[str]]:
        """Return bounded IDs only; PostgreSQL remains the evidence source."""
        depth = max(1, min(depth, 3)); max_nodes = max(1, min(max_nodes, 500))
        rows = self._query(
            "MATCH p=(start:KnowledgeEntity {id: $entity_id, knowledge_base_id: $knowledge_base_id})"
            f"-[*1..{depth}]-(end:KnowledgeEntity) "
            "WHERE all(rel IN relationships(p) WHERE rel.knowledge_base_id = $knowledge_base_id "
            "AND (coalesce(rel.is_legal, false) = false OR coalesce(rel.review_status, 'verified') = 'verified')) "
            "WITH collect(DISTINCT start) + collect(DISTINCT end) AS nodes, "
            "collect(DISTINCT relationships(p)) AS paths "
            "UNWIND nodes AS node "
            "UNWIND paths AS path "
            "UNWIND path AS rel "
            "RETURN collect(DISTINCT node.id)[..$max_nodes] AS node_ids, collect(DISTINCT rel.id) AS relationship_ids",
            {"entity_id": entity_id, "knowledge_base_id": knowledge_base_id, "max_nodes": max_nodes},
        )
        row = (rows[0].get("data") or [{}])[0].get("row", []) if rows else []
        return {"node_ids": row[0] if len(row) > 0 else [], "relationship_ids": row[1] if len(row) > 1 else []}


def project_entity(entity) -> None:
    try:
        Neo4jGraphStore().upsert_entity(entity)
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning("neo4j_entity_projection_failed", extra={"entity_id": entity.id, "error": str(exc)})


def project_relationship(relationship, source, target) -> None:
    try:
        store = Neo4jGraphStore()
        store.upsert_entity(source)
        store.upsert_entity(target)
        store.upsert_relationship(relationship, source, target)
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning("neo4j_relationship_projection_failed", extra={"relationship_id": relationship.id, "error": str(exc)})
