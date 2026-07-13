"""Typed retrieval planning contracts and deterministic intent rules.

The planner is deliberately independent of SQLAlchemy and retrieval engines so
it can be unit tested without infrastructure. Runtime code supplies the
Knowledge Base policy and executes only the channels returned here.
"""
from enum import Enum
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator


class RetrievalChannel(str, Enum):
    VECTOR = "vector"
    FULLTEXT = "full_text"
    GRAPH = "graph"
    LIGHTRAG = "lightrag"


class RetrievalPolicy(BaseModel):
    version: int = Field(default=1, ge=1)
    retrieval_mode: str = "auto"
    enable_vector: bool = True
    enable_fulltext: bool = True
    enable_graph: bool = True
    enable_lightrag: bool = True
    enable_reranker: bool = True
    planner_llm_fallback: bool = True
    default_top_k: int = Field(default=12, ge=1, le=30)
    maximum_top_k: int = Field(default=30, ge=1, le=50)
    maximum_graph_depth: int = Field(default=3, ge=1, le=3)
    citation_required: bool = True

    @field_validator("retrieval_mode")
    @classmethod
    def valid_mode(cls, value: str) -> str:
        if value not in {"auto", "balanced", "precision", "recall"}:
            raise ValueError("retrieval_mode must be auto, balanced, precision, or recall")
        return value


class RetrievalPlan(BaseModel):
    version: int = 1
    intent: str
    channels: list[RetrievalChannel] = Field(default_factory=list)
    max_sources: int = Field(default=12, ge=1, le=50)
    graph_depth: int = Field(default=1, ge=1, le=3)
    planner_source: str = "rules"
    rationale: str = ""
    fallback_reason: str | None = None

    @field_validator("channels")
    @classmethod
    def unique_channels(cls, value: list[RetrievalChannel]) -> list[RetrievalChannel]:
        return list(dict.fromkeys(value))


class PlannerDecision(BaseModel):
    plan: RetrievalPlan
    ambiguous: bool = False
    policy_version: int = 1


def policy_from_config(config: dict[str, Any] | None) -> RetrievalPolicy:
    """Normalize legacy/default JSON without failing a query."""
    try:
        return RetrievalPolicy.model_validate(config or {})
    except Exception:
        return RetrievalPolicy()


def intersect_policies(policies: list[RetrievalPolicy]) -> RetrievalPolicy:
    """Use the most restrictive policy for a multi-KB query.

    This prevents a channel disabled by any selected KB from reading that KB.
    It is intentionally conservative until per-KB sub-executors are introduced.
    """
    if not policies:
        return RetrievalPolicy()
    base = policies[0].model_dump()
    for policy in policies[1:]:
        values = policy.model_dump()
        for key in ("enable_vector", "enable_fulltext", "enable_graph", "enable_lightrag", "enable_reranker", "planner_llm_fallback", "citation_required"):
            base[key] = bool(base[key] and values[key])
        base["default_top_k"] = min(base["default_top_k"], values["default_top_k"])
        base["maximum_top_k"] = min(base["maximum_top_k"], values["maximum_top_k"])
        base["maximum_graph_depth"] = min(base["maximum_graph_depth"], values["maximum_graph_depth"])
    return RetrievalPolicy.model_validate(base)


def _rule_intent(query: str) -> tuple[str, bool]:
    value = query.casefold().strip()
    if any(word in value for word in ("impact", "affected", "ผลกระทบ", "หยุดทำงาน", "กระทบ")):
        return "impact_analysis", False
    if any(word in value for word in ("depend", "relationship", "เชื่อม", "สัมพันธ์", "เกี่ยวข้อง")):
        return "relationship_lookup", False
    if re.search(r"(?:มาตรา|ข้อ|article|section|clause)\s*[0-9๐-๙]+", value):
        return "legal_provision", False
    if re.search(r"\b[a-z]+[-_]?[0-9]{2,}\b", value):
        return "entity_lookup", False
    if len(value.split()) <= 3 or any(word in value for word in ("หา", "ค้น", "search", "where", "which")):
        return "keyword_lookup", False
    return "semantic_hybrid", True


def rule_plan(query: str, policy: RetrievalPolicy, max_sources: int | None = None) -> PlannerDecision:
    intent, ambiguous = _rule_intent(query)
    channels: list[RetrievalChannel] = []
    if policy.retrieval_mode in {"auto", "balanced", "precision", "recall"}:
        if policy.enable_vector and intent not in {"keyword_lookup"}:
            channels.append(RetrievalChannel.VECTOR)
        if policy.enable_fulltext:
            channels.append(RetrievalChannel.FULLTEXT)
        if policy.enable_graph and intent in {"impact_analysis", "relationship_lookup", "legal_provision", "entity_lookup"}:
            channels.append(RetrievalChannel.GRAPH)
        if policy.enable_lightrag and intent in {"semantic_hybrid", "relationship_lookup", "legal_provision"}:
            channels.append(RetrievalChannel.LIGHTRAG)
    if not channels:
        # A policy may intentionally disable every channel. Keep a safe route
        # rather than silently reading a disabled store.
        channels = []
    if policy.retrieval_mode == "precision":
        channels = [channel for channel in channels if channel in {RetrievalChannel.VECTOR, RetrievalChannel.GRAPH, RetrievalChannel.LIGHTRAG}]
    elif policy.retrieval_mode == "recall" and policy.enable_fulltext and RetrievalChannel.FULLTEXT not in channels:
        channels.append(RetrievalChannel.FULLTEXT)
    plan = RetrievalPlan(
        intent=intent,
        channels=channels,
        max_sources=min(max_sources or policy.default_top_k, policy.maximum_top_k),
        graph_depth=policy.maximum_graph_depth if intent in {"impact_analysis", "relationship_lookup"} else 1,
        rationale=f"rule:{intent}",
    )
    return PlannerDecision(plan=plan, ambiguous=ambiguous, policy_version=policy.version)


def apply_llm_plan(decision: PlannerDecision, value: dict[str, Any], policy: RetrievalPolicy,
                   max_sources: int | None = None) -> PlannerDecision:
    """Validate and constrain an LLM plan to the administrator policy."""
    allowed = {
        name for name, enabled in {
            RetrievalChannel.VECTOR.value: policy.enable_vector,
            RetrievalChannel.FULLTEXT.value: policy.enable_fulltext,
            RetrievalChannel.GRAPH.value: policy.enable_graph,
            RetrievalChannel.LIGHTRAG.value: policy.enable_lightrag,
        }.items() if enabled
    }
    requested = [channel for channel in value.get("channels", []) if channel in allowed]
    max_allowed_sources = min(max_sources or policy.default_top_k, policy.maximum_top_k)
    plan = RetrievalPlan(
        intent=str(value.get("intent") or decision.plan.intent)[:80],
        channels=requested,
        max_sources=min(int(value.get("max_sources", max_allowed_sources)), max_allowed_sources),
        graph_depth=min(max(int(value.get("graph_depth", 1)), 1), policy.maximum_graph_depth),
        planner_source="llm",
        rationale="llm:validated and policy-constrained",
    )
    return PlannerDecision(plan=plan, ambiguous=False, policy_version=policy.version)
