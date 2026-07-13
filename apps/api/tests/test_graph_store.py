import json

import httpx

from app.graph_store import Neo4jGraphStore


class Entity:
    id = "entity-1"
    knowledge_base_id = "kb-1"
    name = "APP-01"
    canonical_name = "app-01"
    entity_type = "Application"
    description = "Primary application"


def test_neo4j_projection_uses_parameterized_cypher():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json={"results": [], "errors": []})

    store = Neo4jGraphStore(base_url="http://neo4j.test", username="neo4j", password="secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    store.upsert_entity(Entity())
    payload = json.loads(requests[0].content)
    assert request_path(requests[0]) == "/db/neo4j/tx/commit"
    assert "APP-01" not in payload["statements"][0]["statement"]
    assert payload["statements"][0]["parameters"]["name"] == "APP-01"


def test_neo4j_traverse_returns_ids_only_for_bounded_acceleration():
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"results": [{"data": [{"row": [["entity-1", "entity-2"], ["rel-1"]]}]}], "errors": []})

    store = Neo4jGraphStore(base_url="http://neo4j.test", username="neo4j", password="secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert store.traverse("entity-1", "kb-1", depth=2, max_nodes=20) == {"node_ids": ["entity-1", "entity-2"], "relationship_ids": ["rel-1"]}


def request_path(request: httpx.Request) -> str:
    return request.url.path
