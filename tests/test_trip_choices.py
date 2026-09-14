import asyncio
from datetime import datetime, timedelta, timezone, date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
import httpx
import os

from core.integrations.places.models import VerifiedPlace, GeoPoint, HotelCandidate
from core.integrations.places.service import PlaceInformationService, place_matches_city
from agent_runtime.trip_options import collect_options, options_output
from agent_runtime.engine import Supervisor, Turn
from agent_runtime.contracts import ToolRejected
from agent_runtime.services import SourceScope
from tests.test_supervisor_runtime import FakeServices, FakeStore, CONFIG, SCOPE
from webui_new.routes.chat import _prepare_chat_input
from webui_new.routes.places import create_places_router
from webui_new.schemas.requests import ChatRequest
from webui_new.auth import require_path_user
from webui_new.quick_trip import build_quick_trip_message
from core.integrations.places.amap import AMapProvider


def place(city="重庆市", name="重庆国际会议展览中心"):
    return VerifiedPlace(provider_place_id="P1", name=name, province=city, city=city, district="南岸区", adcode="500108",
        address="南坪北路", location=GeoPoint(lng=106.57, lat=29.54), verified_at=datetime.now(timezone.utc))


def trip(anchor=True):
    start = (date.today() + timedelta(days=1)).isoformat()
    end = (date.today() + timedelta(days=2)).isoformat()
    result = dict(origin="北京", destination="重庆", start_date=start, end_date=end, duration_days=2, trip_purpose="参加会议")
    if anchor:
        result.update(work_location=place().name, work_location_verified=place().model_dump(mode="json"))
    return result


def services(*, train_error=False):
    result = FakeServices()
    result.prefs = {"hotel_brands": ["汉庭"], "seat_preference": "商务座"}
    calls = []
    async def query(origin, destination, day):
        calls.append(("train", origin, destination, day))
        if train_error:
            raise RuntimeError("fixture provider failed")
        return [dict(train_no="G11", from_station="北京西", to_station="重庆北", depart_time="08:00", arrive_time="15:00", duration="07:00", seats={"二等座": "有", "商务座": "无"}),
                dict(train_no="G12", from_station="北京西", to_station="重庆北", depart_time="09:00", arrive_time="17:00", duration="08:00", seats={"二等座": "有", "商务座": "2"})]
    async def hotels(anchor, limit=3, preferred_brands=None):
        calls.append(("hotel", anchor.provider_place_id, limit))
        return [HotelCandidate(provider_place_id=f"H{i}", name="汉庭会展店" if i == 5 else f"酒店 {i}",
            location=GeoPoint(lng=106.57, lat=29.54), distance_m=i*100, retrieved_at=datetime.now(timezone.utc)) for i in range(1, 7)]
    result.trains = SimpleNamespace(query_trains=query)
    result.travel = SimpleNamespace(places=SimpleNamespace(nearby_hotels=hotels))
    async def prepare(current, **kwargs):
        return await collect_options(result, current, **kwargs)
    result.prepare_trip_options = prepare
    return result, calls


@pytest.mark.parametrize("city,expected", [("重庆", True), ("重庆市", True), ("南京", False), ("500000", True), ("", False), ("重庆大学", False)])
def test_city_is_a_constraint(city, expected):
    assert place_matches_city(place(), city) is expected


@pytest.mark.parametrize("name,typecode", [("酒店大堂", "100100"), ("酒店礼宾部", "100102"), ("会展中心", "140300"), ("酒店餐厅", "050100")])
def test_hotel_candidates_exclude_non_lodging_facilities(name, typecode):
    assert AMapProvider._hotel({"id": "X", "name": name, "typecode": typecode, "location": "106.5,29.5"}, datetime.now(timezone.utc)) is None


def test_meeting_place_resolution_is_not_filtered_as_a_hotel():
    assert AMapProvider._place({"id": "X", "name": "会展中心", "typecode": "140300", "location": "106.5,29.5"}) is not None


@pytest.mark.asyncio
async def test_provider_wrong_city_filtered_even_when_upstream_ignores_citylimit():
    class Provider:
        async def search_places(self, keyword, **kwargs):
            return [place(), place("南京市", "南京国际会议中心")]
    found = await PlaceInformationService(Provider()).search("会议中心", city="重庆")
    assert [p.city for p in found] == ["重庆市"]


@pytest.mark.asyncio
async def test_suggestion_endpoint_requires_city_and_filters_results():
    class Places:
        configured = True
        async def search(self, *args, **kwargs):
            return [place(), place("南京市")]
    app = FastAPI()
    app.dependency_overrides[require_path_user] = lambda: SimpleNamespace(id="user")
    app.include_router(create_places_router(Places()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/user/places/suggest", params={"keyword": "会议"})).status_code == 422
        response = await client.get("/api/user/places/suggest", params={"keyword": "会议", "city": "重庆"})
    assert [item["city"] for item in response.json()["items"]] == ["重庆市"]


@pytest.mark.asyncio
async def test_tampered_or_stale_poi_cannot_be_submitted_for_another_city():
    values = trip(False)
    values.update(work_location="冒充的重庆会场", work_location_place_id="P1")
    request = ChatRequest(input_source="quick_trip_form", trip_input=values)
    class Places:
        configured = True
        async def verify(self, place_id):
            return place("南京市")
    with pytest.raises(Exception, match="所选地点不属于目的地"):
        await _prepare_chat_input(request, Places())


@pytest.mark.asyncio
async def test_preferences_rank_full_nearby_pool_and_real_train_rows():
    service, calls = services()
    board = await collect_options(service, trip())
    assert board.hotels[0].hotel.name == "汉庭会展店"
    assert board.hotels[0].matches_brand
    assert ("hotel", "P1", 20) in calls
    assert board.trains[0].train_no == "G12"
    assert board.train_state.status == board.hotel_state.status == "ready"
    assert "票价" in board.train_state.message
    assert options_output(board)["answer_document"]["trip_options"]["anchor"]["provider_place_id"] == "P1"


@pytest.mark.asyncio
async def test_no_selected_poi_still_delivers_trains_and_no_hotel_search():
    service, calls = services()
    board = await collect_options(service, trip(False))
    assert board.trains and board.hotel_state.status == "needs_input"
    assert not any(call[0] == "hotel" for call in calls)
    assert options_output(board)["outcome"] == "waiting_input"


@pytest.mark.asyncio
async def test_train_failure_does_not_remove_hotels_or_anchor():
    service, _ = services(train_error=True)
    board = await collect_options(service, trip())
    assert board.train_state.status == "unavailable"
    assert board.hotels and board.anchor.provider_place_id == "P1"
    assert options_output(board)["outcome"] == "partial"


@pytest.mark.asyncio
async def test_explicit_exclusions_make_no_provider_calls():
    service, calls = services()
    board = await collect_options(service, trip(), selection={"exclude": ["train"]}, user_text="不用查酒店")
    assert not calls
    assert board.train_state.status == board.hotel_state.status == "excluded"


@pytest.mark.asyncio
async def test_ready_form_commits_verified_anchor_then_delivers_without_model():
    service, calls = services()
    store = FakeStore(service)
    fields = trip()
    text = build_quick_trip_message(fields)
    output = await Supervisor(None, service, store, CONFIG).run(SCOPE, text, trip_input=fields)
    assert output["answer_document"]["trip_options"]["hotels"]
    assert service.trip["work_location_verified"]["provider_place_id"] == "P1"
    assert store.writes == 1 and any(call[0] == "train" for call in calls)
    again = await Supervisor(None, service, store, CONFIG).run(SCOPE, text, trip_input=fields)
    assert again["idempotent_replay"] and store.writes == 1


@pytest.mark.asyncio
async def test_read_skill_progressive_reference_and_path_boundary():
    service, _ = services()
    turn = Turn(Supervisor(None, service, FakeStore(service), CONFIG), SCOPE, "规划", "规划", None)
    content = await turn.invoke({}, {"name": "read_skill", "arguments": {"name": "plan-trip", "resource": "references/place-selection.md"}}, None, None, [])
    assert "POI" in content["guidance"]
    with pytest.raises(ToolRejected):
        await turn.invoke({}, {"name": "read_skill", "arguments": {"name": "plan-trip", "resource": "references/../../.env"}}, None, None, [])


@pytest.mark.asyncio
async def test_progressive_skill_reads_do_not_trigger_no_progress_termination():
    from tests.test_supervisor_runtime import reply
    service, _ = services()
    service.trip = trip(False)
    store = FakeStore(service)
    calls = 0
    async def model(*args, **kwargs):
        nonlocal calls
        resources = ["", "references/place-selection.md", "references/travel-choices.md"]
        calls += 1
        if calls <= 3:
            return reply(("read_skill", {"name": "plan-trip", "resource": resources[calls-1]}))
        return reply(("prepare_trip_options", {}))
    output = await Supervisor(model, service, store, CONFIG).run(SCOPE, "继续安排本次出差")
    assert calls == 4 and output["presentation_document"]["place_selection_required"]
    assert not output.get("stop_reason")


@pytest.mark.asyncio
async def test_stale_work_location_is_cleared_when_city_changes():
    service, _ = services()
    service.trip = trip()
    store = FakeStore(service)
    output = await Supervisor(None, service, store, CONFIG).run(SCOPE, "目的地：南京")
    assert service.trip["destination"] == "南京" and service.trip["work_location_verified"] is None
    assert not service.trip["work_location"]
    assert output["presentation_document"]["trip_input"]["destination"] == "南京"
    assert output["presentation_document"]["place_selection_required"]
    assert "work_location_verified" not in output["presentation_document"]["trip_input"]


@pytest.mark.asyncio
async def test_existing_fact_citations_do_not_force_duplicate_mutations():
    service, _ = services()
    service.trip = trip(False)
    turn = Turn(Supervisor(None, service, FakeStore(service), CONFIG), SCOPE, "继续安排", "继续安排", None)
    turn.state = {"trip": service.trip}
    result = await turn.invoke_specialist({"name": "report", "arguments": {"summary": "使用当前行程", "data": {
        "trip": {}, "field_sources": {"destination": "重庆", "duration_days": "共2天"}}}}, "trip_context", SourceScope(), [])
    assert result["_terminal"]["data"]["trip"] == {}
    assert result["_terminal"]["data"]["field_sources"] == {}
    with pytest.raises(ToolRejected, match="遗漏"):
        await turn.invoke_specialist({"name": "report", "arguments": {"summary": "修改目的地", "data": {
            "trip": {}, "field_sources": {"destination": "南京"}}}}, "trip_context", SourceScope(), [])


@pytest.mark.asyncio
async def test_form_clear_invalidates_anchor_and_persists_exclusions():
    from agent_runtime.contracts import Scope
    service, calls = services()
    service.trip = trip()
    store = FakeStore(service)
    fields = trip(False)
    fields.update(work_location="", work_location_place_id="", capability_selection={"include": [], "exclude": ["train"]})
    output = await Supervisor(None, service, store, CONFIG).run(SCOPE, build_quick_trip_message(fields), trip_input=fields)
    assert service.trip["work_location_verified"] is None
    assert not calls and output["presentation_document"]["capability_selection"]["exclude"] == ["train"]
    next_scope = Scope(user_id=SCOPE.user_id, session_id=SCOPE.session_id, request_id="followup-exclusions")
    next_output = await Supervisor(None, service, store, CONFIG).run(next_scope, "工作时间：下午两点开会")
    assert next_output["presentation_document"]["capability_selection"]["exclude"] == ["train"]
    assert not calls


@pytest.mark.skipif(os.getenv("HOMMEY_RUN_LIVE_AGENT_TESTS") != "1", reason="opt-in real model; test store only")
@pytest.mark.asyncio
async def test_live_model_uses_choices_for_a_trip_request():
    from runtime import _generate_kwargs
    from settings import LLM_CONFIG, SUPERVISOR_CONFIG
    from agent_runtime.model_client import create_tool_model
    service, calls = services()
    service.trip = trip(False)
    store = FakeStore(service)
    model = create_tool_model(LLM_CONFIG, _generate_kwargs(LLM_CONFIG), 25)
    result = await Supervisor(model, service, store, SUPERVISOR_CONFIG).run(SCOPE,
        "请帮我安排当前这次出差，在重庆选择准确的会议地点后，给我真实车次和汉庭偏好的附近酒店。")
    checkpoint = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]
    for child in [checkpoint["main"], *checkpoint["children"].values()]:
        print("model_tools", child.get("role", "main"), [t['function']['name'] for m in child["messages"] for t in m.get("tool_calls", [])])
        import json
        print("model_errors", [json.loads(m["content"]) for m in child["messages"] if m.get("role") == "tool" and json.loads(m["content"]).get("error")])
    assert result["presentation_document"]["place_selection_required"]
    assert store.writes == 0 and not calls
    print("live_model_place_intake", result["outcome"])
