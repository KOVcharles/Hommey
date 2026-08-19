from agents.lazy_agent_registry import LazyAgentRegistry
from utils.skill_loader import SkillLoader


EXPECTED_SKILLS = {
    "ask-question",
    "check-trip-compliance",
    "chitchat",
    "event-collection",
    "evaluate-turn",
    "mcp-tool",
    "memory-query",
    "plan-trip",
    "preference",
    "query-info",
    "train-query",
}


EXPECTED_LEGACY_AGENT_NAMES = {
    "rag_knowledge",
    "trip_compliance",
    "memory_query",
    "preference",
    "information_query",
    "itinerary_planning",
    "event_collection",
    "turn_evaluator",
    "chitchat",
    "mcp_tool",
    "train_query",
}


def test_skill_loader_discovers_runtime_skills():
    skills = SkillLoader().load_skills()

    assert set(skills) == EXPECTED_SKILLS


def test_lazy_agent_registry_exposes_skill_and_legacy_names():
    registry = LazyAgentRegistry(model=None, cache={})
    keys = set(registry.keys())

    assert EXPECTED_SKILLS.issubset(keys)
    assert EXPECTED_LEGACY_AGENT_NAMES.issubset(keys)


def test_turn_evaluator_is_discoverable_but_not_intent_backed():
    from core.intent_catalog import SKILL_INTENTS

    definition = SkillLoader().load_definitions()["evaluate-turn"]
    registry = LazyAgentRegistry(model=None, cache={})

    assert definition.agent_name == "turn_evaluator"
    assert definition.intent is None
    assert definition.execution == []
    assert definition.tools == []
    assert definition.user_facing is False
    assert "evaluate-turn" not in {item["skill"] for item in SKILL_INTENTS.values()}
    assert "turn_evaluator" in registry


def test_lazy_registry_passes_explicit_skill_root_to_agent(tmp_path):
    skill_dir = tmp_path / "custom-skill"
    script_dir = skill_dir / "script"
    script_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: custom-skill\ndescription: Verify custom root propagation.\n---\n\n"
        "# Custom Skill\n\nReturn the configured Skill root.\n",
        encoding="utf-8",
    )
    (skill_dir / "hommey.yaml").write_text(
        "intent: custom_intent\n"
        "agent_name: custom_agent\n"
        "execution:\n"
        "  - skill: custom-skill\n"
        "    agent_name: custom_agent\n"
        "    priority: 1\n",
        encoding="utf-8",
    )
    (script_dir / "agent.py").write_text(
        "from agentscope.agent import AgentBase\n"
        "class CustomAgent(AgentBase):\n"
        "    def __init__(self, name='custom_agent', model=None, skills_root=None):\n"
        "        super().__init__()\n"
        "        self.skills_root = skills_root\n",
        encoding="utf-8",
    )

    registry = LazyAgentRegistry(model=None, cache={}, skills_root=str(tmp_path))

    assert registry["custom_agent"].skills_root == str(tmp_path.resolve())
