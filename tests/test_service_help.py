"""Service questions must have a valid exit even amid long business history."""
from copy import deepcopy
import json
import os

import pytest

from agent_runtime.contracts import ToolRejected
from agent_runtime.engine import Supervisor
from agent_runtime.model_client import assistant_message
from agent_runtime.render import service_introduction
from agent_runtime.store import fingerprint
from tests.test_dialogue_routing import ConversationServices, ConversationStore
from tests.test_state_simplification import make_turn
from tests.test_supervisor_runtime import CONFIG, SCOPE, model_payload, outputs, reply
from tests.test_turn_context import policy_result


def long_business_history(text):
    reports = [policy_result(key, task, conclusion) for key, task, conclusion in [
        ("invoice", "电子发票", "电子发票需提供原文件（测试资料）"),
        ("wine", "酒水", "测试制度规定酒水不予报销"),
        ("intern", "实习报销", "测试实习住宿限额为每人每天100元"),
        ("process", "审批流程", "测试审批需项目负责人签字"),
        ("rocket", "火箭报销", "测试资料未查到火箭报销条款"),
        ("expert", "专家劳酬", "测试专家劳酬需发放清册"),
    ]]
    services = ConversationServices()
    # Reproduce polluted assistant answers, while keeping all six reports fresh.
    previous_answer = "\n\n".join("制度检索\n" + r.summary for r in reports) * 3
    for index in range(16):
        services.history.extend([
            {"role": "user", "content": reports[index % len(reports)].task + "怎么报销？"},
            {"role": "assistant", "content": previous_answer},
        ])
    services.history.append({"role": "user", "content": text})
    store = ConversationStore(services)
    store.rows[(SCOPE.user_id, "old")] = {"session": SCOPE.session_id,
        "checkpoint": {"results": {r.result_id: r.model_dump() for r in reports}}}
    return services, store, reports


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_empty_answer", [False, True])
async def test_help_exits_without_reports_queries_or_writes_and_replays(repair_empty_answer):
    text = "你是谁 有啥用  "
    services, store, reports = long_business_history(text)
    calls = 0

    async def model(messages, **kwargs):
        nonlocal calls
        calls += 1
        payload = model_payload(messages)
        assert payload["conversation"][-1]["content"] == text
        assert len(payload["conversation"]) >= 30
        assert len(payload["reference_results"]) == 6 and not payload["work"]
        if repair_empty_answer and calls == 1:
            return reply(("finish", {"kind": "answer"}))
        if repair_empty_answer:
            assert "finish(kind=help)" in outputs(messages)[-1]["error"]
            assert outputs(messages)[-1]["failure"]["fields"] == ["kind", "result_ids"]
        return reply(("finish", {"kind": "help"}))

    runtime = Supervisor(model, services, store, CONFIG)
    result = await runtime.run(SCOPE, text)
    assert calls == (2 if repair_empty_answer else 1)
    assert result["outcome"] == "completed" and "我是 Hommey" in result["response"]
    assert len(result["answer_document"]["sections"]) == 1
    assert result["answer_document"]["sources"] == []
    assert not any(r.summary in result["response"] for r in reports)
    checkpoint = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]
    assert set(checkpoint["results"]) == {r.result_id for r in reports}
    assert checkpoint["work_items"] == {} and checkpoint["children"] == {}
    assert not services.calls and store.writes == 0
    replay = await runtime.run(SCOPE, text)
    assert replay["idempotent_replay"] and replay["response"] == result["response"]


@pytest.mark.asyncio
async def test_help_with_explicit_business_followup_delivers_selected_report_only():
    _, _, reports = long_business_history("")
    turn = make_turn(reports)
    before = deepcopy(turn.state)
    call = {"name": "finish", "arguments": {"kind": "help", "result_ids": ["wine"],
        "reuse_reasons": {"wine": "用户同时要求再列一次刚才酒水的结论"}}}
    with pytest.raises(ToolRejected) as error:
        await turn.invoke_main({}, {**call, "arguments": {"kind": "help", "result_ids": ["wine"]}})
    assert error.value.failure.code == "HISTORICAL_RESULT_NOT_ADOPTED"
    result = (await turn.invoke_main({}, call))["_terminal"]
    assert service_introduction() in result["response"]
    assert reports[1].summary in result["response"] and reports[0].summary not in result["response"]
    assert len(result["answer_document"]["sections"]) == 2
    assert turn.state == before
    turn.state["discarded_results"] = ["wine"]
    with pytest.raises(ToolRejected, match="丢弃"):
        await turn.invoke_main({}, call)
    turn.state["discarded_results"] = []
    turn.state["results"]["wine"]["input_version"] = -1
    with pytest.raises(ToolRejected, match="旧版"):
        await turn.invoke_main({}, call)


@pytest.mark.asyncio
async def test_pending_help_resumes_without_model_or_reexecuting_business():
    text = "你有什么用？"
    services, store, reports = long_business_history(text)
    await store.call("begin", SCOPE, fingerprint({"text": text, "user_text": text}))
    call = {"id": "pending-help", "name": "finish", "arguments": {"kind": "help"}}
    turn = make_turn(reports)
    store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"] = {
        **turn.state, "children": {}, "calls": 0, "preferences_updated": False,
        "control": {"status": "running"}, "main": {"round": 2, "pending": [call], "outputs": {},
            "messages": [{"role": "system", "content": "旧版主提示"},
                {"role": "user", "content": json.dumps({"facts": {}, "work": [],
                    "conversation": [{"role": "user", "content": text}]})}, assistant_message([call])]}}

    async def model(*args, **kwargs):
        raise AssertionError("Pending finish should be resumed without another model call")

    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, text)
    assert result["outcome"] == "completed" and service_introduction() in result["response"]
    assert not services.calls and store.writes == 0


@pytest.mark.skipif(os.getenv("HOMMEY_RUN_LIVE_AGENT_TESTS") != "1", reason="opt-in real model evaluation")
@pytest.mark.asyncio
@pytest.mark.parametrize("text,kind,selected,introduction", [
    ("你是谁 有啥用  ", "help", set(), True),
    ("介绍下自己，能帮我做些什么？", "help", set(), True),
    ("这个助手怎么使用？", "help", set(), True),
    ("你是谁？把刚才酒水的结论也列出来", "help", {"wine"}, True),
    ("把刚才电子发票和酒水的结论一起列出来", "answer", {"invoice", "wine"}, False),
    ("请把前面六项报销咨询的结论全部重新列出来", "answer",
        {"invoice", "wine", "intern", "process", "rocket", "expert"}, False),
    ("酒水那个结论再说一次", "answer", {"wine"}, False),
    ("asdawdasdas", "clarify", set(), False),
])
async def test_live_service_help_in_long_contaminated_history(text, kind, selected, introduction):
    from agent_runtime.model_client import create_tool_model
    from runtime import _generate_kwargs
    from settings import LLM_CONFIG
    services, store, reports = long_business_history(text)
    real_model = create_tool_model(LLM_CONFIG, _generate_kwargs(LLM_CONFIG), 20)
    calls = 0

    async def model(messages, **kwargs):
        nonlocal calls
        calls += 1
        return await real_model(messages, **kwargs)

    result = await Supervisor(model, services, store,
        {**CONFIG, "turn_timeout_sec": 50, "main_rounds": 6}).run(SCOPE, text)
    checkpoint = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]
    trace = [c["function"] for m in checkpoint["main"]["messages"] for c in m.get("tool_calls", [])]
    trace += [v for v in outputs(checkpoint["main"]["messages"]) if v.get("error")]
    trace = json.dumps(trace, ensure_ascii=False)
    assert not services.calls and store.writes == 0, trace
    assert calls <= (4 if selected else 2), trace
    assert result["outcome"] == ("waiting_input" if kind == "clarify" else "completed"), trace
    finish_call = next(c for m in reversed(checkpoint["main"]["messages"])
        for c in m.get("tool_calls", []) if c["function"]["name"] == "finish")
    finish = json.loads(finish_call["function"]["arguments"])
    assert finish.get("kind", "answer") == kind, trace
    assert set(finish.get("result_ids", [])) == selected, trace
    assert set(finish.get("reuse_reasons", {})) == selected, trace
    for report in reports:
        assert (report.summary in result["response"]) == (report.result_id in selected), trace
    assert (service_introduction() in result["response"]) == introduction, trace
