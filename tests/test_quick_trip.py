import asyncio
from datetime import datetime

import pytest
import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from core.integrations.places.models import GeoPoint, VerifiedPlace
from webui_new.quick_trip import build_quick_trip_message, inject_trip_entities
from webui_new.routes.chat import _prepare_chat_input
from webui_new.routes.chat import create_chat_router
from webui_new.auth import require_path_user
from webui_new.schemas.requests import ChatRequest


def _request(**overrides):
    payload = {
        "message": "",
        "input_source": "quick_trip_form",
        "trip_input": {
            "origin": "上海",
            "destination": "杭州",
            "start_date": "2026-09-02",
            "end_date": "2026-09-04",
            "duration_days": 3,
            "trip_purpose": "客户拜访",
            "work_location": "阿里巴巴西溪园区",
            "work_location_place_id": "P1",
        },
        "capability_selection": {
            "include": ["nearby_hotels"],
            "exclude": ["weather"],
        },
    }
    payload.update(overrides)
    return ChatRequest.model_validate(payload)


def _place():
    return VerifiedPlace(
        provider_place_id="P1",
        name="阿里巴巴西溪园区",
        address="文一西路969号",
        city="杭州市",
        district="余杭区",
        location=GeoPoint(lng=120.027, lat=30.279),
        verified_at=datetime.now().astimezone(),
    )


def test_quick_trip_schema_rejects_inconsistent_duration_and_unselected_place():
    with pytest.raises(ValidationError, match="duration_days"):
        _request(trip_input={
            "origin": "上海", "destination": "杭州",
            "start_date": "2026-09-02", "end_date": "2026-09-04",
            "duration_days": 2, "trip_purpose": "客户拜访",
        })
    with pytest.raises(ValidationError, match="work_location_place_id"):
        _request(trip_input={
            "origin": "上海", "destination": "杭州",
            "start_date": "2026-09-02", "end_date": "2026-09-04",
            "trip_purpose": "客户拜访", "work_location": "某园区",
        })


def test_prepare_quick_trip_reverifies_poi_and_builds_grounded_message():
    class FakeService:
        configured = True

        async def verify(self, place_id):
            assert place_id == "P1"
            return _place()

    message, structured, selection = asyncio.run(
        _prepare_chat_input(_request(), FakeService())
    )

    assert "出发地：上海" in message
    assert "工作地点：阿里巴巴西溪园区" in message
    assert "不需要查询：天气" in message
    assert structured["work_location_verified"]["provider_place_id"] == "P1"
    assert selection["include"] == ["nearby_hotels"]


def test_quick_trip_defaults_to_nearby_hotels():
    class FakeService:
        configured = True

        async def verify(self, place_id):
            return _place()

    data = _request(capability_selection=None)
    message, _, selection = asyncio.run(_prepare_chat_input(data, FakeService()))

    assert selection == {"include": ["nearby_hotels"], "exclude": []}
    assert "工作地点附近酒店" in message


def test_inject_trip_entities_only_updates_trip_goal():
    intention = {
        "groups": [
            {"intent": "business_trip_planning", "entities": {"destination": "旧值"}},
            {"intent": "rag_knowledge", "entities": {"topic": "制度"}},
        ],
        "key_entities": {},
    }
    facts = {"origin": "上海", "destination": "杭州", "trip_purpose": "客户拜访"}

    inject_trip_entities(intention, facts)

    assert intention["groups"][0]["entities"]["destination"] == "杭州"
    assert "origin" not in intention["groups"][1]["entities"]
    assert intention["key_entities"]["trip_purpose"] == "客户拜访"


def test_build_quick_trip_message_uses_readable_facts_not_private_control_data():
    message = build_quick_trip_message({
        "origin": "上海", "destination": "杭州", "start_date": "2026-09-02",
        "end_date": "2026-09-04", "duration_days": 3,
        "trip_purpose": "客户拜访", "work_location_place_id": "SECRET-ID",
    })

    assert "上海" in message and "杭州" in message
    assert "SECRET-ID" not in message


def test_quick_trip_chat_route_uses_existing_manager_entrypoint():
    calls = []

    class FakeService:
        configured = True

        async def verify(self, place_id):
            return _place()

    class FakeManager:
        async def process_message(self, user_id, message, **kwargs):
            calls.append((user_id, message, kwargs))
            return {"response": "ok", "agents": [], "preferences_updated": False}

    async def exercise():
        app = FastAPI()
        app.include_router(create_chat_router(FakeManager(), FakeService()))
        app.dependency_overrides[require_path_user] = lambda: object()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            return await client.post(
                "/api/1/chat", json=_request().model_dump(mode="json"),
                headers={"X-Request-ID": "quick-1"},
            )

    response = asyncio.run(exercise())

    assert response.status_code == 200
    assert calls[0][0] == "1"
    assert "请为我规划这次公司差旅" in calls[0][1]
    assert calls[0][2]["structured_trip_input"]["work_location_verified"]["provider_place_id"] == "P1"
