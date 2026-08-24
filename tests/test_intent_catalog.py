"""Tests for the single intent<->skill catalog (core/intent_catalog.py)."""
from utils.skill_loader import SkillLoader
from core.intent_catalog import (
    CHITCHAT_EXACT,
    INTENT_DISPLAY_NAMES,
    NON_SKILL_INTENTS,
    SKILL_INTENTS,
    all_intents,
    build_intent_prompt_section,
    display_name,
    intent_to_skill,
    is_skill_intent,
    is_routable_intent,
    skill_to_intent,
)


def test_chitchat_is_skill_backed():
    assert "chitchat" in SKILL_INTENTS
    assert is_skill_intent("chitchat") is True
    assert intent_to_skill("chitchat") == "chitchat"


def test_smalltalk_is_retired():
    # smalltalk must not exist anywhere in the catalog (replaced by chitchat)
    assert "smalltalk" not in all_intents()
    assert "smalltalk" not in SKILL_INTENTS
    assert "smalltalk" not in NON_SKILL_INTENTS
    assert intent_to_skill("smalltalk") is None


def test_non_skill_intents_are_unclear_and_unsupported():
    assert set(NON_SKILL_INTENTS) == {"unclear", "unsupported"}
    for intent in NON_SKILL_INTENTS:
        assert is_skill_intent(intent) is False
        assert intent_to_skill(intent) is None


def test_intent_skill_roundtrip():
    for intent, info in SKILL_INTENTS.items():
        skill = info["skill"]
        assert intent_to_skill(intent) == skill
        assert skill_to_intent(skill) == intent


def test_display_names_complete():
    for intent in all_intents():
        assert intent in INTENT_DISPLAY_NAMES
        assert display_name(intent)


def test_catalog_matches_discovered_skills():
    """意图目录必须覆盖所有声明了 Hommey intent 的 Skill。"""
    loader = SkillLoader()
    definitions = loader.load_definitions()
    intent_backed = {
        definition.name
        for definition in definitions.values()
        if definition.intent
    }
    catalog_skills = {info["skill"] for info in SKILL_INTENTS.values()}
    assert catalog_skills == intent_backed, (
        f"catalog skills != intent-backed skills. "
        f"missing from catalog: {intent_backed - catalog_skills}; "
        f"missing from skill definitions: {catalog_skills - intent_backed}"
    )


def test_build_intent_prompt_section_lists_intents():
    section = build_intent_prompt_section()
    assert "chitchat" in section
    assert "event_collection" not in section
    assert "unclear" in section
    assert "unsupported" in section
    assert "smalltalk" not in section
    # 非 skill 意图必须带"不调用 skill"标记
    assert "不调用 skill" in section


def test_event_collection_is_internal_but_still_skill_backed():
    assert is_skill_intent("event_collection") is True
    assert is_routable_intent("event_collection") is False
    assert is_routable_intent("itinerary_planning") is True


def test_chitchat_exact_covers_short_greetings():
    # 这些 1~2 字问候必须在 guard 的 length<=2 判断之前命中，否则会被误判为 unclear
    for greeting in ("你好", "在吗", "嗨", "谢谢"):
        assert greeting in CHITCHAT_EXACT
