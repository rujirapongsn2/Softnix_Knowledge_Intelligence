"""Typed, deterministic retrieval planning contracts.

The planner is deliberately independent of SQLAlchemy and retrieval engines so
strategy selection remains testable without indexed content or infrastructure.
"""
from datetime import date
from enum import Enum
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class RetrievalChannel(str, Enum):
    VECTOR = "vector"
    FULLTEXT = "full_text"
    GRAPH = "graph"
    EXACT = "exact_document"
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
    # Legal-registry-aware retrieval. A KB with no legal instruments is
    # unaffected regardless of these values: the resolver finds nothing to
    # match and the fusion boost multiplies against an empty legal_meta map.
    legal_awareness: bool = True
    exclude_invalid: bool = True
    authority_weight: float = Field(default=0.30, ge=0, le=1)
    recency_weight: float = Field(default=0.15, ge=0, le=1)
    status_weight: float = Field(default=0.35, ge=0, le=1)

    @field_validator("retrieval_mode")
    @classmethod
    def valid_mode(cls, value: str) -> str:
        if value not in {"auto", "balanced", "precision", "recall"}:
            raise ValueError("retrieval_mode must be auto, balanced, precision, or recall")
        return value


class LegalContext(BaseModel):
    """Deterministic query-time legal resolution: which instruments were
    matched, which document ids should be treated as current evidence, and
    which document ids are known invalid at the resolved as-of date."""
    matched_instrument_ids: list[str] = Field(default_factory=list)
    current_version_ids: list[str] = Field(default_factory=list)
    amending_instrument_ids: list[str] = Field(default_factory=list)
    excluded_document_ids: list[str] = Field(default_factory=list)
    # When the query names a legal family without an instrument/version, the
    # resolver may select one canonical consolidated expression.  Retrieval
    # uses this list as a hard preference so an amendment's standalone text
    # cannot silently win over the current consolidated text.
    preferred_document_ids: list[str] = Field(default_factory=list)
    provision_refs: list[str] = Field(default_factory=list)
    resolution_notes: list[str] = Field(default_factory=list)
    # A legal provision number is not globally unique.  Never answer by
    # guessing when more than one current candidate remains unresolved.
    ambiguous_context: bool = False
    ambiguity_reason: str | None = None
    candidate_instrument_ids: list[str] = Field(default_factory=list)


class RetrievalPlan(BaseModel):
    version: int = 2
    intent: str
    channels: list[RetrievalChannel] = Field(default_factory=list)
    max_sources: int = Field(default=12, ge=1, le=50)
    graph_depth: int = Field(default=1, ge=1, le=3)
    graph_scope: Literal["none", "local", "global"] = "none"
    entity_subjects: list[str] = Field(default_factory=list)
    document_identifiers: list[str] = Field(default_factory=list)
    published_from: date | None = None
    published_to: date | None = None
    as_of_date: date | None = None
    include_historical: bool = False
    metadata_filters: dict[str, str] = Field(default_factory=dict)
    metadata_document_ids: list[str] | None = Field(default=None, exclude=True, repr=False)
    legal_context: LegalContext | None = None
    authority_weight: float = Field(default=0.30, ge=0, le=1)
    recency_weight: float = Field(default=0.15, ge=0, le=1)
    status_weight: float = Field(default=0.35, ge=0, le=1)
    rerank_enabled: bool = True
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


_DOCUMENT_ID = re.compile(r"\b([A-Za-z]{2,}-\d{4}-\d{3,})\b")
_ENTITY_ID = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*-\d{2,})\b")
_THAI_MONTHS = {
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4, "พฤษภาคม": 5, "มิถุนายน": 6,
    "กรกฎาคม": 7, "สิงหาคม": 8, "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
}
_ENGLISH_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def policy_from_config(config: dict[str, Any] | None) -> RetrievalPolicy:
    try:
        return RetrievalPolicy.model_validate(config or {})
    except Exception:
        return RetrievalPolicy()


def intersect_policies(policies: list[RetrievalPolicy]) -> RetrievalPolicy:
    if not policies:
        return RetrievalPolicy()
    base = policies[0].model_dump()
    for policy in policies[1:]:
        values = policy.model_dump()
        for key in ("enable_vector", "enable_fulltext", "enable_graph", "enable_lightrag", "enable_reranker",
                   "planner_llm_fallback", "citation_required", "legal_awareness", "exclude_invalid"):
            base[key] = bool(base[key] and values[key])
        base["default_top_k"] = min(base["default_top_k"], values["default_top_k"])
        base["maximum_top_k"] = min(base["maximum_top_k"], values["maximum_top_k"])
        base["maximum_graph_depth"] = min(base["maximum_graph_depth"], values["maximum_graph_depth"])
        for key in ("authority_weight", "recency_weight", "status_weight"):
            base[key] = min(base[key], values[key])
    return RetrievalPolicy.model_validate(base)


def _month_range(query: str) -> tuple[date | None, date | None]:
    value = query.casefold()
    match = re.search(r"\b(20\d{2})\b", value)
    if not match:
        return None, None
    year = int(match.group(1))
    month = next((number for name, number in {**_THAI_MONTHS, **_ENGLISH_MONTHS}.items() if name in value), None)
    if not month:
        return None, None
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _rule_details(query: str) -> tuple[str, bool, list[str], list[str], date | None, date | None]:
    value = query.casefold().strip()
    document_ids = _DOCUMENT_ID.findall(query)
    entity_subjects = [item for item in _ENTITY_ID.findall(query) if not any(item in document_id for document_id in document_ids)]
    published_from, published_to = _month_range(query)
    if document_ids:
        return "document_exact", False, entity_subjects, document_ids, published_from, published_to
    if any(word in value for word in ("impact", "affected", "ผลกระทบ", "หยุดทำงาน", "กระทบ", "ล่ม")):
        return "impact_analysis", False, entity_subjects, document_ids, published_from, published_to
    if ("ข่าว" in value or "news" in value) and published_from:
        return "news_by_date", False, entity_subjects, document_ids, published_from, published_to
    if "ภาพรวม" in value and any(word in value for word in ("สัมพันธ์", "relationship", "หน่วยงาน", "organization")):
        return "graph_global", False, entity_subjects, document_ids, published_from, published_to
    if any(word in value for word in ("ปัจจัย", "สาเหตุ", "ล่าช้า", "delay", "cause", "root cause")):
        return "semantic_global", False, entity_subjects, document_ids, published_from, published_to
    if entity_subjects and any(word in value for word in ("depend", "relationship", "เชื่อม", "สัมพันธ์", "เกี่ยวข้อง", "connect")):
        return "relationship_local", False, entity_subjects, document_ids, published_from, published_to
    if re.search(r"(?:มาตรา|ข้อ|article|section|clause)\s*[0-9๐-๙]+", value):
        return "legal_provision", False, entity_subjects, document_ids, published_from, published_to
    if "vpn" in value or any(word in value for word in ("ขั้นตอน", "วิธี", "ทำอย่างไร", "ต้องทำอย่างไร", "ดำเนินการอย่างไร", "how to", "procedure")):
        return "how_to", False, entity_subjects, document_ids, published_from, published_to
    if entity_subjects:
        return "entity_lookup", False, entity_subjects, document_ids, published_from, published_to
    return "semantic_hybrid", True, entity_subjects, document_ids, published_from, published_to


def _enabled(policy: RetrievalPolicy, *channels: RetrievalChannel) -> list[RetrievalChannel]:
    enabled = {
        RetrievalChannel.VECTOR: policy.enable_vector,
        RetrievalChannel.FULLTEXT: policy.enable_fulltext,
        RetrievalChannel.GRAPH: policy.enable_graph,
        RetrievalChannel.EXACT: True,
        RetrievalChannel.LIGHTRAG: policy.enable_lightrag,
    }
    return [channel for channel in channels if enabled[channel]]


def rule_plan(query: str, policy: RetrievalPolicy, max_sources: int | None = None) -> PlannerDecision:
    intent, ambiguous, subjects, document_ids, published_from, published_to = _rule_details(query)
    choices: dict[str, tuple[list[RetrievalChannel], str, int]] = {
        "how_to": ([RetrievalChannel.VECTOR, RetrievalChannel.FULLTEXT], "none", 1),
        "relationship_local": ([RetrievalChannel.GRAPH], "local", 1),
        "entity_lookup": ([RetrievalChannel.VECTOR, RetrievalChannel.FULLTEXT, RetrievalChannel.GRAPH], "local", 1),
        "impact_analysis": ([RetrievalChannel.GRAPH, RetrievalChannel.VECTOR], "local", policy.maximum_graph_depth),
        "semantic_global": ([RetrievalChannel.GRAPH, RetrievalChannel.VECTOR], "global", 1),
        "graph_global": ([RetrievalChannel.GRAPH, RetrievalChannel.VECTOR], "global", 1),
        "news_by_date": ([RetrievalChannel.FULLTEXT, RetrievalChannel.VECTOR], "none", 1),
        "document_exact": ([RetrievalChannel.EXACT, RetrievalChannel.FULLTEXT], "none", 1),
        "legal_provision": ([RetrievalChannel.VECTOR, RetrievalChannel.FULLTEXT, RetrievalChannel.GRAPH, RetrievalChannel.LIGHTRAG], "local", 1),
        "semantic_hybrid": ([RetrievalChannel.VECTOR, RetrievalChannel.FULLTEXT, RetrievalChannel.LIGHTRAG], "none", 1),
    }
    requested, scope, depth = choices[intent]
    channels = _enabled(policy, *requested)
    if policy.retrieval_mode == "precision":
        channels = [channel for channel in channels if channel in {RetrievalChannel.VECTOR, RetrievalChannel.GRAPH, RetrievalChannel.EXACT, RetrievalChannel.LIGHTRAG}]
    elif policy.retrieval_mode == "recall" and policy.enable_fulltext and RetrievalChannel.FULLTEXT not in channels:
        channels.append(RetrievalChannel.FULLTEXT)
    plan = RetrievalPlan(
        intent=intent, channels=channels, max_sources=min(max_sources or policy.default_top_k, policy.maximum_top_k),
        graph_depth=depth, graph_scope=scope, entity_subjects=subjects, document_identifiers=document_ids,
        published_from=published_from, published_to=published_to, rerank_enabled=policy.enable_reranker,
        authority_weight=policy.authority_weight, recency_weight=policy.recency_weight, status_weight=policy.status_weight,
        rationale=f"rule:{intent}",
    )
    return PlannerDecision(plan=plan, ambiguous=ambiguous, policy_version=policy.version)


def apply_llm_plan(decision: PlannerDecision, value: dict[str, Any], policy: RetrievalPolicy,
                   max_sources: int | None = None, query: str = "") -> PlannerDecision:
    """Constrain a fallback plan while preserving deterministic extracted fields."""
    allowed = {channel.value for channel in _enabled(policy, *list(RetrievalChannel))}
    requested = [channel for channel in value.get("channels", []) if channel in allowed]
    # Postgres full-text search cannot tokenize unspaced Thai text (no default
    # Thai dictionary), so an LLM plan that drops the vector channel for a Thai
    # query strands it on a channel that returns zero rows even when the KB
    # holds the answer. Keep the semantic channel in that case.
    thai = sum(1 for ch in query if "\u0e00" <= ch <= "\u0e7f") / max(len(query), 1)
    if thai >= 0.2 and "vector" in allowed and "vector" not in requested:
        requested.append("vector")
    max_allowed_sources = min(max_sources or policy.default_top_k, policy.maximum_top_k)
    plan = decision.plan.model_copy(update={
        "intent": str(value.get("intent") or decision.plan.intent)[:80],
        "channels": requested,
        "max_sources": min(int(value.get("max_sources", max_allowed_sources)), max_allowed_sources),
        "graph_depth": min(max(int(value.get("graph_depth", 1)), 1), policy.maximum_graph_depth),
        "rerank_enabled": policy.enable_reranker,
        "planner_source": "llm",
        "rationale": "llm:validated and policy-constrained",
    })
    return PlannerDecision(plan=plan, ambiguous=False, policy_version=policy.version)
