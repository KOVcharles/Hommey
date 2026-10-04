"""Repeated observations must not keep an otherwise idle supervisor alive."""
from copy import deepcopy
import json

import pytest

from agent_runtime.context_window import compact_tool_history
from agent_runtime.contracts import SpecialistResult
from agent_runtime.control import degraded_output, made_progress
from agent_runtime.engine import Supervisor, Turn
from agent_runtime.loop_detection import MAX_OBSERVATIONS, record_tool_outcome
from agent_runtime.model_client import assistant_message, tool_message
from agent_runtime.services import SourceScope
from agent_runtime.store import safe_checkpoint, trip_version
from tests.test_supervisor_control import role_of, weather_report
from tests.test_supervisor_runtime import CONFIG, SCOPE, FakeServices, FakeStore, outputs, reply


def make_turn(content="已核实的信息"):
    services = FakeServices()
    turn = Turn(Supervisor(None, services, FakeStore(services), CONFIG), SCOPE, "查询", "查询", None)
    sources = SourceScope()
    ref = sources.add("memory", {"content": content})["source_id"]
    result = SpecialistResult(result_id="result_0123456789abcdef", role="memory", task="查询",
        summary="已核实的信息", input_version=trip_version({}), sources=list(sources.sources.values()))
    turn.state = {"trip": {}, "version": result.input_version, "results": {result.result_id: result.model_dump()},
                  "discarded_results": [], "applied_results": [], "main": {"outputs": {}, "round": 0}}
    async def save(*args, **kwargs):
        pass
    turn.save = save
    return turn, ref, result.result_id


async def read(turn, name, arguments, *, call_id="read", new_round=True):
    state = turn.state["main"]
    if new_round:
        state["round"] += 1
        state["outputs"] = {}
    call = {"id": call_id, "name": name, "arguments": arguments}
    value = await turn.invoke_cached(state, call, None, None, [])
    return call, value


@pytest.mark.asyncio
async def test_default_arguments_and_call_ids_do_not_disguise_repeated_reads():
    turn, ref, _ = make_turn()
    first_call, first = await read(turn, "read_source", {"source_id": ref}, call_id="first")
    call, repeated = await read(turn, "read_source", {"limit": 4000, "offset": 0, "source_id": ref}, call_id="second")
    assert made_progress(first_call, first) and not made_progress(call, repeated)
    assert repeated["data"] == first["data"] and "finish" in repeated["loop_warning"]
    assert len(turn.state["main"]["tool_observations"]) == 1


@pytest.mark.asyncio
async def test_result_updates_and_validity_changes_are_new_observations():
    turn, _, result_id = make_turn()
    args = {"result_id": result_id}
    await read(turn, "read_result", args)
    call, duplicate = await read(turn, "read_result", args)
    assert not made_progress(call, duplicate)
    turn.state["results"][result_id]["data"] = {"findings": [{"conclusion": "新增要求"}]}
    call, updated = await read(turn, "read_result", args)
    assert made_progress(call, updated)
    turn.state["discarded_results"].append(result_id)
    call, discarded = await read(turn, "read_result", args)
    assert discarded["discarded"] and made_progress(call, discarded)


@pytest.mark.asyncio
async def test_new_pages_are_progress_but_revisiting_old_pages_is_not():
    turn, ref, _ = make_turn("长资料" * 2000)
    call, first = await read(turn, "read_source", {"source_id": ref})
    assert made_progress(call, first) and first["next_offset"] == 4000
    call, second = await read(turn, "read_source", {"source_id": ref, "offset": first["next_offset"]})
    assert made_progress(call, second) and second["data_fragment"]
    call, repeated = await read(turn, "read_source", {"source_id": ref})
    assert repeated["data_fragment"] == first["data_fragment"] and not made_progress(call, repeated)


@pytest.mark.asyncio
async def test_source_payload_and_expiry_changes_are_progress_but_fetch_time_is_not():
    turn, ref, result_id = make_turn()
    args = {"source_id": ref}
    await read(turn, "read_source", args)
    source = turn.state["results"][result_id]["sources"][0]
    source["retrieved_at"] = "2000-01-01T00:00:00+00:00"
    call, repeated = await read(turn, "read_source", args)
    assert not made_progress(call, repeated)
    source["data"]["content"] = "新证据"
    call, updated = await read(turn, "read_source", args)
    assert made_progress(call, updated)
    source["kind"] = "weather"
    call, expired = await read(turn, "read_source", args)
    assert expired["expired"] and made_progress(call, expired)


@pytest.mark.asyncio
async def test_compaction_and_durable_resume_keep_evidence_readable_and_repetition_known():
    turn, ref, _ = make_turn("资料" * 1000 + "联系电话 13812345678")
    call, first = await read(turn, "read_source", {"source_id": ref})
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "查询"},
                assistant_message([call]), tool_message(call, first),
                assistant_message([{"id": "last", "name": "read_result", "arguments": {}}])]
    assert compact_tool_history(messages, target_chars=500) > 0
    assert "data" not in json.loads(messages[3]["content"])
    resumed, _, _ = make_turn()
    # Match the production redaction and JSON serialization boundary.
    resumed.state = json.loads(json.dumps(safe_checkpoint(turn.state)))
    call, reread = await read(resumed, "read_source", {"source_id": ref})
    assert reread["data"]["content"].startswith("资料")
    assert not made_progress(call, reread) and "13812345678" not in reread["data"]["content"]


@pytest.mark.asyncio
async def test_pending_output_replay_does_not_record_the_same_execution_twice():
    turn, ref, _ = make_turn()
    args = {"source_id": ref}
    call, first = await read(turn, "read_source", args)
    saved = deepcopy(turn.state)
    resumed, _, _ = make_turn()
    resumed.state = saved
    _, replay = await read(resumed, "read_source", args, new_round=False)
    assert replay == first and made_progress(call, replay)
    assert list(resumed.state["main"]["tool_observations"].values())[0]["count"] == 1
    call, repeated = await read(resumed, "read_source", args)
    assert not made_progress(call, repeated)


@pytest.mark.asyncio
async def test_same_batch_duplicates_do_not_hide_a_new_observation():
    turn, ref, result_id = make_turn()
    _, first = await read(turn, "read_source", {"source_id": ref}, call_id="one")
    call, duplicate = await read(turn, "read_source", {"source_id": ref}, call_id="two", new_round=False)
    assert duplicate["repeated_observation"] and not made_progress(call, duplicate)
    call, new = await read(turn, "read_result", {"result_id": result_id}, call_id="three", new_round=False)
    assert made_progress(call, new) and first["data"]


def test_history_is_bounded_and_jsonb_key_order_does_not_change_eviction():
    state = {}
    value = {"id": "source", "data": {"content": "same"}}
    for i in range(MAX_OBSERVATIONS):
        record_tool_outcome(state, "read_source", str(i), value, terminal_tool="finish")
    # PostgreSQL JSONB need not retain insertion order.
    state["tool_observations"] = dict(reversed(list(state["tool_observations"].items())))
    record_tool_outcome(state, "read_source", "new", value, terminal_tool="finish")
    assert len(state["tool_observations"]) == MAX_OBSERVATIONS
    assert not any(key.startswith("0:") for key in state["tool_observations"])
    assert record_tool_outcome(state, "read_source", "1", value, terminal_tool="finish")["repeated_observation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern, expected_rounds", [("source", 5), ("result", 5), ("alternating", 6)])
async def test_uncooperative_main_stops_repeated_or_alternating_reads_before_round_limit(pattern, expected_rounds):
    rounds = 0
    async def model(messages, **kwargs):
        nonlocal rounds
        out = outputs(messages)
        if role_of(messages) is None:
            rounds += 1
            if not out:
                return reply(("delegate", {"role": "travel_info", "task": "上海天气"}))
            result = out[0]
            if pattern == "result":
                return reply(("read_result", {"result_id": result["result_id"]}))
            index = (rounds - 2) % 2 if pattern == "alternating" else 0
            return reply(("read_source", {"source_id": result["evidence_refs"][index]}))
        if not out:
            return reply(("get_weather", {"city": "上海"}), ("get_weather", {"city": "北京"}))
        refs = [v["sources"][0]["source_id"] for v in out[:2]]
        if len(out) == 2:
            return reply(*[("read_source", {"source_id": ref}) for ref in refs])
        report = weather_report(refs[0])
        report["data"]["findings"].append({"item": "北京天气", "conclusion": "北京晴", "evidence_refs": [refs[1]]})
        report["evidence_refs"] = refs
        return reply(("report", report))

    class TwoCities(FakeServices):
        async def execute(self, scope, name, args):
            kind, data = await super().execute(scope, name, args)
            return kind, {**data, "city": args.city}

    services = TwoCities()
    store = FakeStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, "上海天气")
    state = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]["main"]
    assert result["stop_reason"] == "NO_PROGRESS" and rounds == expected_rounds
    assert state["no_progress"] == 3 and result["answer_document"]["sources"]
    assert len(services.calls) == 2 and store.writes == 0


@pytest.mark.asyncio
async def test_warning_allows_model_to_finish_normally_and_new_turn_starts_fresh():
    async def model(messages, **kwargs):
        out = outputs(messages)
        if role_of(messages) is None:
            if not out:
                return reply(("delegate", {"role": "travel_info", "task": "上海天气"}))
            if out[-1].get("repeated_observation"):
                assert "finish" in out[-1]["loop_warning"] and out[-1]["data"]
                return reply(("finish", {"result_ids": [out[0]["result_id"]]}))
            return reply(("read_source", {"source_id": out[0]["evidence_refs"][0]}))
        if not out:
            return reply(("get_weather", {"city": "上海"}))
        ref = out[0]["sources"][0]["source_id"]
        if len(out) == 1:
            return reply(("read_source", {"source_id": ref}))
        return reply(("report", weather_report(ref)))

    class PreviousStore(FakeStore):
        async def call(self, method, scope, *args):
            if method == "previous" and scope.request_id != SCOPE.request_id:
                return deepcopy(self.rows[(SCOPE.user_id, SCOPE.request_id)])
            return await super().call(method, scope, *args)

    services = FakeServices()
    store = PreviousStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, "上海天气")
    assert result["outcome"] == "completed" and "stop_reason" not in result
    assert store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]["main"]["round"] == 4
    previous = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]["main"]["messages"]
    async def history_context(scope):
        return {"trip": services.trip, "recent": deepcopy(previous), "current_excluded": True}
    services.context = history_context
    async def followup(messages, **kwargs):
        current = max(i for i, m in enumerate(messages) if m["role"] == "user")
        out = outputs(messages[current:])
        if not out:
            work = [r for r in outputs(messages[:current]) if "result_id" in r]
            return reply(("read_result", {"result_id": work[0]["result_id"]}))
        assert not out[-1].get("repeated_observation")
        return reply(("finish", {"result_ids": [out[-1]["result_id"]],
            "reuse_reasons": {out[-1]["result_id"]: "用户要求继续查看上一轮天气"}}))
    result = await Supervisor(followup, services, store, CONFIG).run(
        SCOPE.model_copy(update={"request_id": "next"}), "继续查看")
    assert result["outcome"] == "completed" and len(services.calls) == 1


def test_round_limit_notice_identifies_the_actual_limit():
    assert "步骤轮数上限" in degraded_output([], "SUPERVISOR_ROUND_LIMIT")["response"]
