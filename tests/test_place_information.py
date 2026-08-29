import asyncio
import json

import httpx
from agentscope.message import Msg

from agents.lazy_agent_registry import LazyAgentRegistry
from core.integrations.places.amap import AMapProvider
from core.integrations.places.models import GeoPoint, HotelCandidate, ReferenceCost, VerifiedPlace
from core.orchestration.fallback_composer import FallbackComposer
from core.orchestration.models import IntentTask, TaskResult


def _place(place_id="P1", name="阿里巴巴西溪园区"):
    from datetime import datetime

    return VerifiedPlace(
        provider_place_id=place_id,
        name=name,
        address="杭州市余杭区文一西路969号",
        city="杭州市",
        district="余杭区",
        adcode="330110",
        citycode="0571",
        location=GeoPoint(lng=120.027, lat=30.279),
        verified_at=datetime.now().astimezone(),
    )


def test_amap_provider_normalizes_and_limits_nearby_hotels():
    async def handler(request: httpx.Request):
        assert request.url.path == "/v3/place/around"
        return httpx.Response(200, json={
            "status": "1",
            "pois": [
                {
                    "id": f"H{index}", "name": f"酒店{index}",
                    "address": f"示例路{index}号", "adname": "余杭区",
                    "location": f"120.0{index},30.2{index}",
                    "distance": str(distance),
                    "biz_ext": {"rating": str(rating), "cost": cost},
                }
                for index, distance, rating, cost in (
                    (1, 900, 4.5, "520"),
                    (2, 300, 4.2, []),
                    (3, 600, 4.8, "680"),
                    (4, 1200, 4.9, "800"),
                )
            ],
        })

    client = httpx.AsyncClient(
        base_url="https://restapi.amap.com",
        transport=httpx.MockTransport(handler),
    )
    provider = AMapProvider(
        config={
            "enabled": True,
            "api_key": "test-key",
            "base_url": "https://restapi.amap.com",
            "timeout_sec": 1,
            "hotel_radius_m": 5000,
        },
        client=client,
    )
    try:
        hotels = asyncio.run(provider.nearby_hotels(_place(), limit=3))
    finally:
        asyncio.run(client.aclose())

    assert [hotel.name for hotel in hotels] == ["酒店2", "酒店3", "酒店1"]
    assert len(hotels) == 3
    assert hotels[0].reference_cost is None
    assert hotels[0].price_status == "unknown"
    assert hotels[1].reference_cost.amount == 680
    assert hotels[1].reference_cost.realtime is False


def test_place_agent_uses_trip_work_location_and_returns_three_hotels():
    class FakeService:
        configured = True

        async def resolve_anchor(self, keyword, *, city=""):
            assert keyword == "阿里巴巴西溪园区"
            assert city == "杭州市"
            place = _place()
            return place, [place]

        async def nearby_hotels(self, anchor, *, limit=3):
            from datetime import datetime

            return [
                HotelCandidate(
                    provider_place_id=f"H{index}",
                    name=f"候选酒店{index}",
                    address=f"示例路{index}号",
                    location=GeoPoint(lng=120.02 + index / 1000, lat=30.27),
                    distance_m=index * 100,
                    rating=4.0 + index / 10,
                    reference_cost=ReferenceCost(amount=400 + index * 50),
                    price_status="reference_only",
                    retrieved_at=datetime.now().astimezone(),
                )
                for index in range(1, 4)
            ]

    agent = LazyAgentRegistry(model=None, cache={})["place_information"]
    agent.service = FakeService()
    payload = {
        "context": {"active_task": {
            "query": "核验工作地点并查询附近酒店",
            "entities": {},
            "capabilities": ["nearby_hotels"],
        }},
        "previous_results": [{
            "agent_name": "event_collection",
            "result": {"data": {
                "destination": "杭州市",
                "work_location": "阿里巴巴西溪园区",
                "planning_ready": True,
            }},
        }],
    }

    reply = asyncio.run(agent.reply(Msg(
        name="Orchestrator", content=json.dumps(payload, ensure_ascii=False), role="user",
    )))
    result = json.loads(reply.content)

    assert result["query_success"] is True
    assert len(result["results"]["hotels"]) == 3
    assert result["results"]["hotels"][0]["reference_cost"]["realtime"] is False
    assert "不是指定日期实时房价" in result["results"]["price_notice"]


def test_place_agent_does_not_use_city_center_without_work_location():
    class FakeService:
        configured = True

    agent = LazyAgentRegistry(model=None, cache={})["place_information"]
    agent.service = FakeService()
    payload = {
        "context": {"active_task": {
            "query": "核验本次出差的工作地点并查询附近酒店",
            "entities": {"destination": "杭州市"},
            "capabilities": ["nearby_hotels"],
        }},
        "previous_results": [],
    }

    reply = asyncio.run(agent.reply(Msg(
        name="Orchestrator", content=json.dumps(payload, ensure_ascii=False), role="user",
    )))
    result = json.loads(reply.content)

    assert result["query_success"] is True
    assert result["skipped"] is True
    assert result["results"]["hotels"] == []


def test_place_agent_reuses_server_verified_form_anchor_without_name_search():
    class FakeService:
        configured = True

        async def resolve_anchor(self, keyword, *, city=""):
            raise AssertionError("verified POI must not be resolved by name again")

        async def nearby_hotels(self, anchor, *, limit=3):
            assert anchor.provider_place_id == "P1"
            return []

    agent = LazyAgentRegistry(model=None, cache={})["place_information"]
    agent.service = FakeService()
    payload = {
        "context": {"active_task": {
            "query": "核验工作地点并查询附近酒店",
            "entities": {
                "work_location": "阿里巴巴西溪园区",
                "work_location_verified": _place().model_dump(mode="json"),
            },
            "capabilities": ["nearby_hotels"],
        }},
        "previous_results": [],
    }

    reply = asyncio.run(agent.reply(Msg(
        name="Orchestrator", content=json.dumps(payload, ensure_ascii=False), role="user",
    )))
    result = json.loads(reply.content)

    assert result["query_success"] is True
    assert result["results"]["anchor"]["provider_place_id"] == "P1"


def test_standalone_place_result_renders_as_nearby_hotels():
    hotel = {
        "provider_place_id": "H1",
        "name": "西溪候选酒店",
        "address": "杭州市余杭区示例路1号",
        "distance_m": 420,
        "rating": 4.6,
        "reference_cost": {"amount": 520, "realtime": False},
    }
    task = IntentTask(
        task_id="information_query", intent="information_query",
        query="阿里巴巴西溪园区附近有哪些酒店？",
    )
    results = [
        TaskResult(
            task_id="information_query-information_query",
            goal_id="information_query", intent="information_query",
            agent_name="information_query", status="success",
            data={"query_success": True, "skipped": True},
        ),
        TaskResult(
            task_id="information_query-place_information",
            goal_id="information_query", intent="information_query",
            agent_name="place_information", status="success",
            data={"query_success": True, "results": {
                "hotels": [hotel],
                "price_notice": "高德参考消费不是指定日期实时房价或可订库存。",
            }},
        ),
    ]

    document = FallbackComposer().compose([task], results)

    assert document.sections[0].title == "附近酒店"
    assert document.sections[0].items[0].label == "西溪候选酒店"
    assert "520" in document.sections[0].items[0].value
    assert "实时房价" in document.sections[0].body


def test_itinerary_agent_attaches_provider_hotels_after_model_output():
    async def model(_messages):
        return {"content": json.dumps({
            "itinerary": {
                "title": "杭州出差",
                "duration": "3天",
                "transport_recommendation": {},
                "lodging_advice": "优先选择工作地点附近",
                "daily_plans": [],
                "notes": [],
                "reimbursement_checklist": [],
                "missing_info": [],
            },
            "planning_complete": True,
        }, ensure_ascii=False)}

    agent = LazyAgentRegistry(model=model, cache={})["itinerary_planning"]
    payload = {
        "context": {"active_task": {"query": "规划杭州出差"}},
        "previous_results": [{
            "agent_name": "place_information",
            "result": {"data": {"results": {"hotels": [{
                "provider_place_id": "H1",
                "name": "西溪候选酒店",
                "address": "杭州市余杭区示例路1号",
                "distance_m": 420,
                "rating": 4.6,
                "reference_cost": {
                    "amount": 520, "currency": "CNY", "source": "amap",
                    "realtime": False, "label": "高德参考消费",
                },
                "price_status": "reference_only",
                "source": "amap",
            }]}}},
        }],
    }

    reply = asyncio.run(agent.reply(Msg(
        name="Orchestrator", content=json.dumps(payload, ensure_ascii=False), role="user",
    )))
    result = json.loads(reply.content)

    hotels = result["itinerary"]["hotel_recommendations"]
    assert [item["name"] for item in hotels] == ["西溪候选酒店"]
    assert hotels[0]["reference_cost"]["realtime"] is False
    assert any("不是指定日期实时房价" in note for note in result["itinerary"]["notes"])
