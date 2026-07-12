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

    def upsert_entity(self, entity) -> None:
        self._execute(
            "MERGE (node:KnowledgeEntity {id: $id}) "
            "SET node.knowledge_base_id = $knowledge_base_id, node.name = $name, "
            "node.canonical_name = $canonical_name, node.entity_type = $entity_type, "
            "node.description = $description, node.updated_at = datetime()",
            {"id": entity.id, "knowledge_base_id": entity.knowledge_base_id, "name": entity.name,
             "canonical_name": entity.canonical_name, "entity_type": entity.entity_type,
             "description": entity.description},
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
            "edge.description = $description, edge.confidence = $confidence, edge.updated_at = datetime()",
            {"id": relationship.id, "source_id": source.id, "target_id": target.id,
             "knowledge_base_id": relationship.knowledge_base_id, "relationship_type": relationship.relationship_type,
             "description": relationship.description, "confidence": relationship.confidence},
        )


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
