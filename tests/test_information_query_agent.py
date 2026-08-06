from pathlib import Path

from agents.lazy_agent_registry import LazyAgentRegistry


def test_information_query_skill_is_registered():
    registry = LazyAgentRegistry(model=None, cache={})

    assert "information_query" in registry
    assert "query-info" in registry.keys()


def test_information_query_skill_has_agent_script():
    script_path = Path(".agents/skills/query-info/script/agent.py")

    assert script_path.exists()
