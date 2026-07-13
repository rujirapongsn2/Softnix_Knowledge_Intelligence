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
