"""Regression for the reported Chongqing intake request, including model failure."""
import pytest

from agent_runtime.contracts import SpecialistResult, WorkItem
from agent_runtime.engine import Supervisor, Turn, is_direct_policy_query
from agent_runtime.intake_submission import parse_trip_entry
from agent_runtime.store import trip_version
from tests.test_supervisor_runtime import CONFIG, SCOPE, FakeServices, FakeStore, outputs, reply
from tests.test_supervisor_control import role_of


@pytest.mark.asyncio
@pytest.mark.parametrize("text,origin", [("我要去重庆出差", None), ("我准备去重庆出差。", None), ("我要从北京去重庆出差", "北京")])
async def test_explicit_trip_entry_saves_literal_places_and_returns_form_without_model(text, origin):
    services = FakeServices()
    store = FakeStore(services)
    async def model(*args, **kwargs):
        raise AssertionError("A simple incomplete trip must not wait for the model")
    runtime = Supervisor(model, services, store, CONFIG)
    result = await runtime.run(SCOPE, text)
    card = result["presentation_document"]
    assert result["answer_document"] is None and result["outcome"] == "waiting_input"
    assert card["type"] == "trip_intake" and card["route"]["destination"] == "重庆"
    assert card["progress"] == {"completed": 2 if origin else 1, "total": 6}
    missing = {f["key"] for f in card["missing_required"]}
    assert missing == ({"start_date", "trip_length", "trip_purpose"} if origin else {"origin", "start_date", "trip_length", "trip_purpose"})
    assert not services.trip.get("start_date") and not services.trip.get("trip_purpose")
    assert services.trip.get("origin") == origin and store.writes == 1
    assert result["public_plan"]["steps"][0]["status"] == "needs_input"
    replay = await runtime.run(SCOPE, text)
    assert replay["presentation_document"] == card and store.writes == 1


@pytest.mark.parametrize("text", ["我要去重庆出差，看看差旅标准", "我要去重庆出差，查一下天气",
    "我不去重庆出差", "如果下雨我要去重庆出差", "我要去重庆和成都出差", "我要明天去重庆出差", "我要去重庆出差吗？"])
def test_entry_parser_does_not_drop_other_intents_or_qualifiers(text):
    assert parse_trip_entry(text) is None


def test_policy_request_still_uses_policy_route():
    assert is_direct_policy_query("我要去重庆出差，看看差旅标准")


@pytest.mark.asyncio
async def test_existing_chongqing_destination_is_reused_without_duplicate_write():
    services = FakeServices()
    services.trip = {"destination": "重庆", "status": "active"}
    store = FakeStore(services)
    result = await Supervisor(None, services, store, CONFIG).run(SCOPE, "我要去重庆出差")
    assert store.writes == 0
    assert result["outcome"] == "waiting_input"
    assert result["presentation_document"]["progress"] == {"completed": 1, "total": 6}


@pytest.mark.asyncio
async def test_model_false_success_and_empty_finish_question_still_produce_form():
    text = "请帮我安排一次到重庆的出差"
    services = FakeServices()
    store = FakeStore(services)
    main_calls = 0
    async def model(messages, **kwargs):
        nonlocal main_calls
        if role_of(messages) == "trip_context":
            return reply(("report", {"status": "success", "summary": "已整理重庆出差", "missing_info": [],
                "data": {"trip": {"destination": "重庆"}, "field_sources": {"destination": text}}}))
        main_calls += 1
        if not outputs(messages):
            return reply(("delegate", {"role": "trip_context", "task": text}))
        return reply(("finish", {"kind": "ask"}))
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, text)
    assert main_calls == 2 and store.writes == 1
    assert result["presentation_document"]["route"]["destination"] == "重庆"
    assert result["outcome"] == "waiting_input" and not result.get("stop_reason")
    checkpoint = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]
    extraction = next(iter(checkpoint["results"].values()))
    assert extraction["status"] == "needs_input" and "origin" in extraction["missing_info"]
    assert not any(o.get("error") for o in outputs(checkpoint["main"]["messages"]))


def test_legacy_successful_trip_result_does_not_disable_fallback_form():
    services = FakeServices()
    turn = Turn(Supervisor(None, services, FakeStore(services), CONFIG), SCOPE, "我要去重庆出差", "我要去重庆出差", None)
    trip = {"destination": "重庆"}
    version = trip_version(trip)
    result = SpecialistResult(result_id="result_old", role="trip_context", task="整理行程", status="success",
        summary="已整理", input_version=version, data={"trip": trip})
    turn.state = {"trip": trip, "version": version, "results": {result.result_id: result.model_dump()},
        "applied_results": [result.result_id], "discarded_results": [],
        "work_items": {"old": WorkItem(role="trip_context", task="整理行程", result_id=result.result_id,
            input_version=version, status="completed").model_dump()}}
    output = turn.fallback("NO_PROGRESS")
    assert output["presentation_document"]["type"] == "trip_intake"
    assert output["presentation_document"]["route"]["destination"] == "重庆"
    assert output["answer_document"] is None


@pytest.mark.asyncio
async def test_real_stream_adapter_delivers_intake_document_and_waiting_status():
    from webui_new.manager import HommeyWebInstance
    services = FakeServices()
    runtime = Supervisor(None, services, FakeStore(services), CONFIG)
    instance = object.__new__(HommeyWebInstance)
    async def process(message, **kwargs):
        return await runtime.run(SCOPE, message, progress=kwargs.get("progress_callback"))
    instance.process_message = process
    events = [event async for event in instance.stream_message("我要去重庆出差", request_id=SCOPE.request_id)]
    cards = [e for e in events if e["type"] == "presentation_document"]
    assert len(cards) == 1 and cards[0]["document"]["route"]["destination"] == "重庆"
    assert events[-1]["type"] == "done" and events[-1]["outcome"] == "waiting_input"
    assert not any(e["type"] in {"answer_document", "error"} for e in events)
