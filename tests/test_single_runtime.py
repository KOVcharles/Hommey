"""Offline checks for the sole runtime factory and guidance-only Skill catalog."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_runtime.profiles import PROFILES
from core.skill_definition import load_skill_definition
from utils.skill_loader import SkillLoader
from webui_new.skill_platform.service import SkillPlatformService


def test_every_specialist_has_loadable_guidance_and_read_only_catalog():
    loader = SkillLoader()
    service = SkillPlatformService(loader)
    catalog = {item["name"]: item for item in service.list_skills()}
    assert len(PROFILES) == 6
    for role, profile in PROFILES.items():
        for name in profile.skills:
            assert role in catalog[name]["roles"]
            assert service.get_skill(name)["instructions"]
    assert "mcp-tool" not in catalog and "chitchat" not in catalog
    assert service.get_skill("../../settings.py") is None


def test_skill_cannot_register_an_executor(tmp_path):
    skill = tmp_path / "sample"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: sample\ndescription: Travel guidance\n---\nRead policy evidence.", encoding="utf-8")
    (skill / "hommey.yaml").write_text("entrypoint: arbitrary.py\n", encoding="utf-8")
    with pytest.raises(ValueError, match="entrypoint"):
        load_skill_definition(skill)


def test_factory_builds_one_model_and_one_supervisor(monkeypatch):
    import runtime
    import agentscope.model

    raw = Mock()
    model_factory = Mock(return_value=raw)
    pool = object()
    memory = SimpleNamespace(long_term=SimpleNamespace(pool=pool))
    memory_factory = Mock(return_value=memory)
    services = object()
    store = object()
    supervisor = object()
    supervisor_factory = Mock(return_value=supervisor)
    monkeypatch.setattr(runtime, "init_agentscope", lambda: None)
    monkeypatch.setattr(agentscope.model, "OpenAIChatModel", model_factory)
    monkeypatch.setattr(runtime, "MemoryManager", memory_factory)
    monkeypatch.setattr(runtime, "BusinessServices", lambda manager: services)
    monkeypatch.setattr(runtime, "RunStore", lambda value: store)
    monkeypatch.setattr(runtime, "Supervisor", supervisor_factory)
    monkeypatch.setattr(runtime, "get_shared_attachment_service", lambda: None)

    built = runtime.create_agent_runtime("employee", "session")
    model_factory.assert_called_once()
    memory_factory.assert_called_once_with(user_id="employee", session_id="session")
    supervisor_factory.assert_called_once_with(built.model, services, store, runtime.SUPERVISOR_CONFIG)
    assert built.supervisor is supervisor
    assert set(vars(built)) == {"model", "memory_manager", "supervisor", "attachment_service"}
