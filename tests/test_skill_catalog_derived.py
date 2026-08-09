"""声明式编排元数据契约：core/intent_catalog 派生注册表 + guard_rules。

约束单一数据源：一切编排元数据（section kind、progress_key、side_effect、
pause、memory hooks、result_rules、置信度门槛）都必须能从
`.agents/skills/*/hommey.yaml` 派生，新增 skill 零 Python 改动。
"""
from core.guard_rules import transaction_supported
from core.intent_catalog import (
    SKILL_INTENTS,
    agent_for_intent,
    catalog_rank,
    pause_spec_for_intent,
    confidence_threshold_for_intent,
    execution_steps_for_intent,
    intent_to_skill,
    memory_hooks_for_intent,
    primary_agent_for_intent,
    progress_key_for_intent,
    require_section_for_intent,
    result_rules_for_intent,
    section_kind_for_intent,
    side_effect_allowed,
    suppress_agents_for_intent,
    updates_preferences_for_agent,
)


def test_every_intent_skill_declares_section_kind_and_progress_key():
    for intent in SKILL_INTENTS:
        assert section_kind_for_intent(intent), f"{intent} 缺少 answer.section_kind"
        assert progress_key_for_intent(intent), f"{intent} 缺少 progress_key"


def test_section_kind_is_one_of_seven_answer_kinds():
    allowed = {"policy", "weather", "memory", "preference", "trip", "notice", "general"}
    for intent in SKILL_INTENTS:
        assert section_kind_for_intent(intent) in allowed, intent


def test_agent_for_intent_roundtrips():
    from core.intent_catalog import intent_to_skill, skill_to_intent

    for intent in SKILL_INTENTS:
        skill = intent_to_skill(intent)
        assert skill_to_intent(skill) == intent
        assert agent_for_intent(intent), f"{intent} 缺少 agent_name"


def test_unknown_intent_derived_lookups_return_none():
    assert agent_for_intent("no_such_intent") is None
    assert section_kind_for_intent("no_such_intent") is None
    assert execution_steps_for_intent("no_such_intent") == []
    assert memory_hooks_for_intent("no_such_intent") == []


def test_plain_skills_get_defaults():
    # chitchat / mcp-tool 未声明 confidence_threshold / scope
    assert confidence_threshold_for_intent("chitchat") is None
    assert require_section_for_intent("chitchat") is True


def test_plan_trip_declares_pause_and_side_effect():
    spec = pause_spec_for_intent("itinerary_planning")
    assert spec is not None
    assert spec.enabled is True
    assert spec.pause_agent == "event_collection"
    assert spec.pause_field == "planning_ready"
    assert side_effect_allowed("itinerary_planning") is True
    assert primary_agent_for_intent("itinerary_planning") == "itinerary_planning"
    assert suppress_agents_for_intent("itinerary_planning") == [
        "event_collection", "rag_knowledge", "information_query",
    ]


def test_plan_trip_execution_template_matches_declared_steps():
    steps = execution_steps_for_intent("itinerary_planning")
    assert [(s.agent_name, s.priority, s.on_failure) for s in steps] == [
        ("event_collection", 1, "abort"),
        ("rag_knowledge", 2, "abort"),
        ("information_query", 2, "continue"),
        ("itinerary_planning", 3, "abort"),
        ("trip_compliance", 4, "continue"),
    ]
    # 每个步骤都有 scoped query 模板（防跨意图污染）
    for step in steps:
        assert step.query, f"{step.agent_name} 缺少 query 模板"


def test_memory_hooks_are_agent_owned():
    event_hooks = memory_hooks_for_intent("event_collection")
    assert [(h.agent, h.effect) for h in event_hooks] == [
        ("event_collection", "update_active_trip"),
    ]
    plan_hooks = memory_hooks_for_intent("itinerary_planning")
    assert [(h.agent, h.effect, h.require_field) for h in plan_hooks] == [
        ("itinerary_planning", "complete_trip", "itinerary"),
    ]
    preference_hooks = memory_hooks_for_intent("preference")
    assert [(h.agent, h.effect) for h in preference_hooks] == [
        ("preference", "save_preference"),
    ]


def test_updates_preferences_is_declarative():
    assert updates_preferences_for_agent("preference") is True
    assert updates_preferences_for_agent("rag_knowledge") is False
    assert updates_preferences_for_agent("no_such_agent") is False


def test_query_info_result_rules_replace_executor_special_case():
    rules = result_rules_for_intent("information_query")
    assert rules == {
        "error_when_field": "query_success",
        "error_code": "INFORMATION_QUERY_UNAVAILABLE",
    }
    assert result_rules_for_intent("rag_knowledge") == {}


def test_scope_forbidden_and_expansion_terms_are_declarative():
    from core.intent_catalog import _definition_for_intent

    rag = _definition_for_intent("rag_knowledge")
    assert "天气" in rag.scope.forbidden_terms
    info = _definition_for_intent("information_query")
    assert "报销" in info.scope.forbidden_terms


def test_catalog_rank_is_sorted_stable():
    ranks = [catalog_rank(intent) for intent in SKILL_INTENTS]
    assert ranks == sorted(ranks)


def test_transaction_keywords_are_catalog_derived():
    # 当前目录没有消费交易语言的意图 → transaction_supported() 为 False。
    # （guard 对订票请求放行给 LLM 判定；加入 ticket skill 后该探测变 True。）
    assert transaction_supported() is False
    from core.guard_rules import TRANSACTION_INTENTS
    assert not any(intent in SKILL_INTENTS for intent in TRANSACTION_INTENTS)


def test_intent_api_payload_is_declarative():
    from core.intent_catalog import intent_api_payload

    payload = intent_api_payload()
    assert set(payload) == set(SKILL_INTENTS)
    for intent, meta in payload.items():
        assert meta["display"]
        assert meta["progress_key"]
        assert meta["description"]
        assert meta["skill"] == intent_to_skill(intent)


def test_supports_task_pipeline_accepts_all_skill_intents():
    from core.orchestration.validator import supports_task_pipeline

    for intent in SKILL_INTENTS:
        data = {
            "intents": [{"type": intent, "confidence": 0.9, "should_call_skill": True}]
        }
        assert supports_task_pipeline(data), f"{intent} 应可进入 task pipeline"
    # 非 skill 意图（unclear/unsupported）不可进入。
    for intent in ("unclear", "unsupported", "no_such_intent"):
        data = {"intents": [{"type": intent, "confidence": 0.9, "should_call_skill": True}]}
        assert supports_task_pipeline(data) is False, intent


def test_future_transaction_skill_unblocks_guard():
    # 验证"纯声明式扩展"闭环：一旦目录声明交易意图，transaction_supported 变 True。
    # （不真的建目录，直接模拟 is_skill_intent 的判定输入）
    from unittest import mock

    from core.guard_rules import TRANSACTION_INTENTS

    candidate = next(iter(TRANSACTION_INTENTS))
    assert candidate  # 至少有一个保留的意图名
    with mock.patch("core.guard_rules.is_skill_intent") as fake:
        fake.side_effect = lambda intent: intent == candidate
        assert transaction_supported() is True
