import asyncio
import json
from pathlib import Path

import pytest
from agents.lazy_agent_registry import LazyAgentRegistry
from agents.orchestration_agent import OrchestrationAgent
from agentscope.message import Msg

from context.long_term_memory import FileLongTermMemory
from core.schedule_builder import build_agent_schedule
from utils.skill_loader import SkillLoader
from webui_new.auth.deps import require_admin
from webui_new.auth.storage import User
from webui_new.core.errors import BusinessError
from webui_new.manager import HommeyWebInstance
from webui_new.skill_platform import SkillPlatformService
from webui_new.routes.skill_admin import create_skill_admin_router


class _Store:
    configured = True

    def __init__(self):
        self.values = {}

    def settings(self):
        return self.values

    def recent_runs(self, limit=100, skill_name=None):
        return []

    def set_enabled(self, skill_name, enabled, updated_by):
        self.values[skill_name] = {"enabled": enabled, "updated_by": updated_by}


class _DisabledStore(_Store):
    configured = False

    def is_enabled(self, skill_name, default=True):
        return skill_name != "ask-question"


class _ReplyAgent:
    def __init__(self, name, payload):
        self.name = name
        self.payload = payload
        self.calls = 0

    async def reply(self, _message):
        self.calls += 1
        return Msg(name=self.name, content=json.dumps(self.payload), role="assistant")


def test_every_skill_has_standard_metadata_and_valid_runtime_config():
    loader = SkillLoader()
    skills = loader.load_skills()
    definitions = loader.load_definitions()

    assert set(skills) == set(definitions)
    assert definitions["plan-trip"].category == "workflow"
    assert definitions["check-trip-compliance"].risk_level == "high"
    assert "rag_retrieval" in definitions["check-trip-compliance"].tools


def test_skill_md_frontmatter_is_the_discovery_metadata_source():
    loader = SkillLoader()
    skills = loader.load_skills()
    definitions = loader.load_definitions()

    for name, definition in definitions.items():
        raw_skill = (loader.skills_dir / name / "SKILL.md").read_text(encoding="utf-8")
        procedure = loader.get_skill_content(name)

        assert raw_skill.startswith("---\n")
        assert procedure
        assert not procedure.startswith("---")
        assert skills[name]["name"] == definition.name
        assert skills[name]["description"] == definition.description
        assert skills[name]["directory"] == definition.name


def test_standard_skill_without_hommey_extension_is_discoverable(tmp_path):
    skill_dir = tmp_path / "plain-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: plain-skill\n"
        "description: A portable Agent Skill used for loader compatibility tests.\n"
        "---\n\n"
        "# Plain Skill\n\nFollow the requested portable workflow.\n",
        encoding="utf-8",
    )

    loader = SkillLoader(str(tmp_path))
    definition = loader.load_definitions()["plain-skill"]

    assert definition.description.startswith("A portable Agent Skill")
    assert definition.intent is None
    assert definition.agent_name is None
    assert definition.display_name == "plain-skill"
    assert definition.hommey_configured is False


def test_hommey_extension_cannot_redefine_standard_metadata(tmp_path):
    skill_dir = tmp_path / "plain-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: plain-skill\ndescription: Portable metadata.\n---\n\n# Instructions\n",
        encoding="utf-8",
    )
    (skill_dir / "hommey.yaml").write_text(
        "description: Conflicting runtime metadata.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="description"):
        SkillLoader(str(tmp_path)).load_definitions()


def test_skill_resource_reader_stays_inside_package():
    loader = SkillLoader()

    rules = loader.get_skill_resource(
        "check-trip-compliance",
        "references/evidence-rules.md",
    )

    assert "If two sources conflict" in rules
    with pytest.raises(ValueError, match="escapes package root"):
        loader.get_skill_resource("check-trip-compliance", "../plan-trip/SKILL.md")


def test_plan_workflow_is_declarative_and_ends_with_compliance():
    schedule = build_agent_schedule([{"type": "itinerary_planning"}])

    assert [(item["agent_name"], item["priority"]) for item in schedule] == [
        ("event_collection", 1),
        ("rag_knowledge", 2),
        ("information_query", 2),
        ("itinerary_planning", 3),
        ("trip_compliance", 4),
    ]


def test_disabled_skill_is_removed_before_orchestration():
    orchestrator = OrchestrationAgent(agent_registry={}, skill_store=_DisabledStore())

    filtered, disabled = orchestrator._filter_enabled_schedule(
        [
            {"agent_name": "rag_knowledge", "priority": 1},
            {"agent_name": "itinerary_planning", "priority": 2},
        ]
    )

    assert filtered == [{"agent_name": "itinerary_planning", "priority": 2}]
    assert disabled == ["ask-question"]


def test_thin_reply_executes_schedule_and_returns_raw_results():
    agents = {
        "event_collection": _ReplyAgent("event_collection", {"destination": "南京"}),
        "rag_knowledge": _ReplyAgent("rag_knowledge", {"answer": "住宿标准"}),
    }
    orchestrator = OrchestrationAgent(agent_registry=agents)

    response = asyncio.run(
        orchestrator.reply(
            Msg(
                name="intention",
                content=json.dumps({
                    "agent_schedule": [
                        {"agent_name": "event_collection", "priority": 1},
                        {"agent_name": "rag_knowledge", "priority": 1},
                    ],
                }),
                role="assistant",
            )
        )
    )
    data = json.loads(response.content)

    assert data["status"] == "completed"
    assert [item["agent_name"] for item in data["results"]] == [
        "event_collection",
        "rag_knowledge",
    ]


def test_thin_reply_does_not_auto_expand_workflow():
    # 暂停/续跑语义已移交给 DAG 管线：薄壳只执行给定 schedule，不做模板回放。
    event_collection = _ReplyAgent("event_collection", {"planning_ready": True, "origin": "北京", "destination": "南昌"})
    itinerary_planning = _ReplyAgent("itinerary_planning", {"itinerary": {"title": "南昌出差", "daily_plans": []}})
    orchestrator = OrchestrationAgent(agent_registry={
        "event_collection": event_collection,
        "itinerary_planning": itinerary_planning,
    })

    response = asyncio.run(
        orchestrator.reply(
            Msg(
                name="intention",
                content=json.dumps({
                    "agent_schedule": [{"agent_name": "event_collection", "priority": 1}],
                }),
                role="assistant",
            )
        )
    )
    data = json.loads(response.content)

    assert [item["agent_name"] for item in data["results"]] == ["event_collection"]
    assert itinerary_planning.calls == 0


def test_completed_plan_hides_workflow_intermediates():
    from core.orchestration.fallback_composer import FallbackComposer
    from core.orchestration.models import IntentTask, TaskResult

    task = IntentTask(
        task_id="itinerary_planning",
        intent="itinerary_planning",
        query="规划广州出差行程",
        entities={"destination": "广州"},
        display_order=0,
    )

    def _res(agent, data):
        return TaskResult(
            task_id=f"itinerary_planning-{agent}",
            intent="itinerary_planning",
            agent_name=agent,
            status="success",
            data=data,
            display_order=0,
        )

    results = [
        _res("event_collection", {"destination": "广州"}),
        _res("rag_knowledge", {"answer": "不应展示的中间制度回答"}),
        _res("information_query", {"results": {"summary": "不应展示的中间外部信息"}}),
        _res("itinerary_planning", {"itinerary": {"title": "广州两天出差", "daily_plans": []}}),
        _res("trip_compliance", {"verdict": "unknown", "summary": "制度证据不足"}),
    ]

    document = FallbackComposer().compose([task], results)

    joined = document.plain_text + json.dumps(
        [section.model_dump() for section in document.sections], ensure_ascii=False
    )
    assert "广州两天出差" in joined
    assert "不应展示的中间制度回答" not in joined
    assert "不应展示的中间外部信息" not in joined
    # 合规结论以 notice 浮出（不在 trip 主 section 中隐藏）。
    assert document.notices


def test_compliance_skill_refuses_a_definite_verdict_without_rag_evidence():
    registry = LazyAgentRegistry(model=None, cache={})
    agent = registry["trip_compliance"]
    response = asyncio.run(
        agent.reply(
            Msg(
                name="Orchestrator",
                content=json.dumps(
                    {
                        "context": {"active_trip": {"destination": "南京"}},
                        "previous_results": [
                            {
                                "agent_name": "event_collection",
                                "result": {"data": {"origin": "上海", "destination": "南京"}},
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                role="user",
            )
        )
    )
    data = json.loads(response.content)

    assert data["status"] == "insufficient_evidence"
    assert data["verdict"] == "unknown"
    assert data["sources"] == []


def test_file_memory_keeps_one_incrementally_updated_active_trip(tmp_path):
    memory = FileLongTermMemory("u1", storage_path=str(tmp_path))

    memory.upsert_active_trip({"destination": "南京", "missing_info": ["出发地"]})
    updated = memory.upsert_active_trip({"origin": "上海", "missing_info": []})

    assert updated["destination"] == "南京"
    assert updated["origin"] == "上海"
    assert memory.get_active_trip()["missing_info"] == []


def test_skill_platform_service_exposes_dependency_graph():
    service = SkillPlatformService(store=_Store())

    skills = service.list_skills()
    graph = service.dependency_graph()

    assert any(item["name"] == "check-trip-compliance" for item in skills)
    assert any(
        edge["source"] == "check-trip-compliance" and edge["target"] == "plan-trip"
        for edge in graph["edges"]
    )


def test_require_admin_rejects_normal_user_and_accepts_admin():
    normal = User(1, "user@example.com", "hash", "now", role="user")
    admin = User(2, "admin@example.com", "hash", "now", role="admin")

    with pytest.raises(BusinessError) as exc:
        asyncio.run(require_admin(normal))
    assert exc.value.status_code == 403
    assert asyncio.run(require_admin(admin)) == admin


def test_skill_platform_migration_is_non_destructive():
    sql = Path("webui_new/auth/migrations/0002_skill_platform.sql").read_text(encoding="utf-8")

    assert "active_trip_contexts" in sql
    assert "skill_settings" in sql
    assert "skill_execution_runs" in sql
    assert "DROP TABLE" not in sql.upper()


def test_admin_skill_api_registers_management_routes():
    store = _Store()
    service = SkillPlatformService(store=store)
    router = create_skill_admin_router(service)

    paths = {(route.path, tuple(sorted(route.methods))) for route in router.routes}
    assert ("/api/admin/skills", ("GET",)) in paths
    assert ("/api/admin/skills/{skill_name}", ("GET",)) in paths
    assert ("/api/admin/skills/{skill_name}/enabled", ("PATCH",)) in paths
