import asyncio
import json

from agents.lazy_agent_registry import LazyAgentRegistry
from agentscope.message import Msg
from core.orchestration.fallback_composer import FallbackComposer
from core.orchestration.models import IntentTask, TaskResult
from utils.skill_loader import SkillLoader


def test_trip_shaped_model_output_is_normalized_to_itinerary_contract():
    registry = LazyAgentRegistry(model=None, cache={})
    module = registry["itinerary_planning"].__class__.__module__
    normalize = __import__(module, fromlist=["normalize_planning_result"]).normalize_planning_result

    result = normalize(
        {
            "status": "success",
            "trip": {
                "origin": "北京",
                "destination": "南昌",
                "duration_days": 2,
                "transport_recommendation": {"mode": "高铁", "advice": "优先高铁"},
                "lodging_advice": {"hotel": "汉庭", "location_advice": "靠近客户地点"},
                "daily_schedule": [{"date": "2026-07-14", "activities": [{"time": "09:00", "description": "拜访客户"}]}],
            },
        }
    )

    assert result["planning_complete"] is True
    assert result["itinerary"]["title"] == "北京至南昌出差行程"
    assert result["itinerary"]["daily_plans"][0]["activities"][0]["activity"] == "拜访客户"


def test_plan_trip_skill_is_business_focused_and_advice_only():
    content = SkillLoader().get_skill_content("plan-trip")

    assert "公司差旅" in content
    assert "不得编造真实车次" in content
    assert "不执行预订、付款、审批" in content
    assert "reimbursement_checklist" in content
    assert "必须给出具体的景点" not in content


def test_itinerary_agent_receives_business_trip_constraints():
    prompts = []
    payload = {
        "itinerary": {
            "title": "南京公司差旅方案",
            "duration": "2天",
            "transport_recommendation": {
                "preferred": "高铁",
                "reason": "耗时稳定",
                "verification": "请以铁路官方渠道为准",
            },
            "daily_plans": [],
            "reimbursement_checklist": ["交通票据", "住宿发票"],
            "missing_info": ["出发日期"],
        },
        "planning_complete": False,
    }

    async def fake_model(messages):
        prompts.append(messages[0]["content"])
        return json.dumps(payload, ensure_ascii=False)

    registry = LazyAgentRegistry(model=fake_model, cache={})
    agent = registry["itinerary_planning"]
    response = asyncio.run(
        agent.reply(
            Msg(
                name="Orchestrator",
                content=json.dumps(
                    {"context": {"rewritten_query": "帮我规划去南京出差的路线"}},
                    ensure_ascii=False,
                ),
                role="user",
            )
        )
    )
    data = json.loads(response.content)

    assert data["itinerary"]["transport_recommendation"]["preferred"] == "高铁"
    assert "不得编造真实车次" in prompts[0]
    assert "仅提供建议" in prompts[0]


def test_itinerary_plain_text_shows_transport_and_lodging_advice():
    task = IntentTask(
        task_id="itinerary_planning",
        intent="itinerary_planning",
        query="规划南京出差行程",
        entities={"destination": "南京"},
        display_order=0,
    )
    result = TaskResult(
        task_id="itinerary_planning",
        intent="itinerary_planning",
        agent_name="itinerary_planning",
        status="success",
        data={
            "itinerary": {
                "title": "南京公司差旅方案",
                "duration": "2天",
                "transport_recommendation": {
                    "preferred": "高铁",
                    "reason": "耗时稳定",
                    "verification": "最终核验车次和余票",
                },
                "lodging_advice": "住在会议地点附近",
                "daily_plans": [],
                "reimbursement_checklist": ["交通票据", "住宿发票"],
                "estimated_budget": "待确认公司标准后估算",
                "missing_info": ["出发地"],
            }
        },
        display_order=0,
    )

    document = FallbackComposer().compose([task], [result])

    assert document.sections[0].kind == "trip"
    items = {item.label: item.value for item in document.sections[0].items}
    assert items.get("首选交通") == "高铁"
    assert items.get("住宿建议") == "住在会议地点附近"
    assert items.get("预算参考") == "待确认公司标准后估算"
    assert "高铁" in document.plain_text
    assert "南京" in document.title
