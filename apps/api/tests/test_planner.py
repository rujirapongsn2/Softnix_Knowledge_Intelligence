from datetime import date

import pytest

from app.planner import RetrievalChannel, RetrievalPolicy, apply_llm_plan, rule_plan


def test_rule_plan_selects_graph_for_legal_provision_and_respects_policy():
    policy = RetrievalPolicy(enable_lightrag=False, maximum_graph_depth=2)
    decision = rule_plan("What does มาตรา 30 require?", policy, 10)
    assert decision.plan.intent == "legal_provision"
    assert RetrievalChannel.GRAPH in decision.plan.channels
    assert RetrievalChannel.LIGHTRAG not in decision.plan.channels
    assert decision.plan.graph_depth == 1


def test_ambiguous_plan_is_policy_constrained_after_llm_fallback():
    policy = RetrievalPolicy(enable_graph=False, enable_fulltext=True, enable_vector=True)
    initial = rule_plan("Explain the tradeoffs for our platform", policy, 20)
    assert initial.ambiguous is True
    decision = apply_llm_plan(initial, {"intent": "relationship_lookup", "channels": ["graph", "vector", "full_text"], "max_sources": 99, "graph_depth": 9}, policy, 20)
    assert decision.plan.planner_source == "llm"
    assert decision.plan.channels == [RetrievalChannel.VECTOR, RetrievalChannel.FULLTEXT]
    assert decision.plan.max_sources == 20
    assert decision.plan.graph_depth == 3


def test_policy_disabling_all_channels_does_not_reenable_a_store():
    decision = rule_plan("search", RetrievalPolicy(enable_vector=False, enable_fulltext=False, enable_graph=False, enable_lightrag=False), 10)
    assert decision.plan.channels == []


@pytest.mark.parametrize("query", [
    "การโอนกรรมสิทธิ์หรือสิทธิครอบครองในที่ดินต้องทำอย่างไร?",
    "ต้องดำเนินการอย่างไรเพื่อขอสิทธิ์ VPN",
])
def test_thai_how_to_phrases_use_vector_and_full_text(query):
    plan = rule_plan(query, RetrievalPolicy(enable_lightrag=False), 10).plan
    assert plan.intent == "how_to"
    assert [channel.value for channel in plan.channels] == ["vector", "full_text"]


def test_legal_provision_takes_precedence_over_how_to_phrase():
    plan = rule_plan("มาตรา 7 ต้องทำอย่างไร", RetrievalPolicy(enable_lightrag=False), 10).plan
    assert plan.intent == "legal_provision"


def test_how_to_takes_precedence_over_generic_entity_lookup():
    plan = rule_plan("APP-01 ต้องทำอย่างไร", RetrievalPolicy(enable_lightrag=False), 10).plan
    assert plan.intent == "how_to"
    assert plan.entity_subjects == ["APP-01"]


@pytest.mark.parametrize(("query", "intent", "channels", "scope", "depth", "subject", "document_id", "published_from"), [
    ("ขั้นตอนขอสิทธิ์ VPN คืออะไร", "how_to", ["vector", "full_text"], "none", 1, None, None, None),
    ("APP-01 เชื่อมต่อกับระบบใด", "relationship_local", ["graph"], "local", 1, "APP-01", None, None),
    ("ระบบใดได้รับผลกระทบหาก APP-01 ล่ม", "impact_analysis", ["graph", "vector"], "local", 3, "APP-01", None, None),
    ("ปัจจัยหลักที่ทำให้โครงการล่าช้า", "semantic_global", ["graph", "vector"], "global", 1, None, None, None),
    ("ข่าวบริษัท ABC เดือนมิถุนายน 2026", "news_by_date", ["full_text", "vector"], "none", 1, None, None, date(2026, 6, 1)),
    ("เอกสารเลขที่ SNX-2026-001", "document_exact", ["exact_document", "full_text"], "none", 1, None, "SNX-2026-001", None),
    ("ภาพรวมความสัมพันธ์ระหว่างหน่วยงาน", "graph_global", ["graph", "vector"], "global", 1, None, None, None),
])
def test_auto_retrieval_contract_for_expected_questions(query, intent, channels, scope, depth, subject, document_id, published_from):
    plan = rule_plan(query, RetrievalPolicy(enable_lightrag=False, maximum_graph_depth=3)).plan
    assert plan.intent == intent
    assert [channel.value for channel in plan.channels] == channels
    assert plan.graph_scope == scope and plan.graph_depth == depth
    assert (plan.entity_subjects[0] if plan.entity_subjects else None) == subject
    assert (plan.document_identifiers[0] if plan.document_identifiers else None) == document_id
    assert plan.published_from == published_from
    if published_from:
        assert plan.published_to == date(2026, 7, 1)


def test_llm_plan_keeps_vector_channel_for_thai_queries():
    """Postgres FTS cannot tokenize unspaced Thai; an LLM plan that drops the
    vector channel for a Thai query must be corrected (E2E finding)."""
    policy = RetrievalPolicy(enable_vector=True, enable_fulltext=True, enable_graph=False, enable_lightrag=False)
    initial = rule_plan("ปริมาณน้ำฝนกรุงเทพ", policy, 10)
    assert initial.ambiguous is True
    decision = apply_llm_plan(initial, {"intent": "semantic_hybrid", "channels": ["full_text"]}, policy, 10, query="ปริมาณน้ำฝนกรุงเทพ")
    assert RetrievalChannel.VECTOR in decision.plan.channels


def test_llm_plan_respects_dropped_vector_for_english_queries():
    policy = RetrievalPolicy(enable_vector=True, enable_fulltext=True, enable_graph=False, enable_lightrag=False)
    initial = rule_plan("explain the platform tradeoffs", policy, 10)
    decision = apply_llm_plan(initial, {"intent": "semantic_hybrid", "channels": ["full_text"]}, policy, 10, query="explain the platform tradeoffs")
    assert RetrievalChannel.VECTOR not in decision.plan.channels
