"""Natural main-agent replies and non-destructive context projection."""
from copy import deepcopy
import json
import os

import pytest

from agent_runtime.context_window import conversation_window, main_model_context
from agent_runtime.contracts import Finish
from agent_runtime.engine import Supervisor
from agent_runtime.model_client import assistant_message, call_model, tool_message
from agent_runtime.render import render
from agent_runtime.store import fingerprint, trip_version
from tests.test_dialogue_routing import ConversationServices, ConversationStore
from tests.test_supervisor_runtime import CONFIG, SCOPE, FakeServices, FakeStore, outputs, reply
from tests.test_turn_context import policy_result


def text_reply(text):
    return {"content": [{"type": "text", "text": text}]}


def followup_fixture():
    # Fictional policy evidence, no production messages or external data sources.
    report = policy_result("two_cities", "南京和上海标准比较",
        "测试资料：科研经费下，两地在相同职级时住宿上限相同；非科研经费住宿金额未查到，不能确认相同。")
    report.missing_info = ["非科研经费在两地的住宿金额"]
    previous = render(Finish(result_ids=[report.result_id]), [report])["response"]
    services = ConversationServices()
    services.history = [
        {"role": "user", "content": "查一下南京和上海的标准"},
        {"role": "assistant", "content": previous, "result_ids": [report.result_id]},
        {"role": "user", "content": "两个地方都是这个标准吗？"},
    ]
    store = ConversationStore(services)
    store.rows[(SCOPE.user_id, "old")] = {"session": SCOPE.session_id,
        "checkpoint": {"results": {report.result_id: report.model_dump()}}}
    return services, store, report, previous


def test_report_links_survive_windowing_without_copying_report_body():
    services, _, report, previous = followup_fixture()
    rows = deepcopy(services.history)
    rows[0]["result_ids"] = ["user_cannot_supply_report_links"]
    conversation = conversation_window(rows, rows[-1]["content"])
    assert "result_ids" not in conversation[0]
    stored = [{"role": "system", "content": "规则"}, {"role": "user", "content": json.dumps({
        "conversation": conversation, "work": [{"result_id": report.result_id,
        "stale": False, "expired": False, "discarded": False, "committed": True}]}, ensure_ascii=False)}]
    before = deepcopy(stored)
    messages = main_model_context(stored, set())
    assert messages[2]["content"] == "已交付报告 two_cities 的内容：\n" + previous
    assert sum(m.get("content", "").count(previous) for m in messages) == 1
    from tests.test_supervisor_runtime import model_payload
    assert model_payload(messages)["result_status"] == [
        {"result_id": "two_cities", "committed": True, "usable": True}]
    assert stored == before


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_terminal", [False, True])
async def test_followup_answers_once_without_reading_or_replaying_report(resume_terminal):
    services, store, report, previous = followup_fixture()
    calls = []
    answer = "科研经费下，同职级的两地住宿上限相同；非科研经费还缺两地金额，不能确认一样。"

    async def model(messages, **kwargs):
        calls.append(deepcopy(messages))
        assert kwargs["tool_choice"] == "required"
        assert messages[-1]["content"] == services.history[-1]["content"]
        assert "已交付报告 two_cities 的内容：" in messages[-2]["content"]
        assert previous in messages[-2]["content"]
        assert not outputs(messages)
        return text_reply(answer)

    runtime = Supervisor(model, services, store, CONFIG)
    output = await runtime.run(SCOPE, services.history[-1]["content"])
    assert output["response"] == answer != previous
    assert output["answer_document"] is None and output["outcome"] == "completed"
    row = store.rows[(SCOPE.user_id, SCOPE.request_id)]
    assert row["checkpoint"]["main"]["messages"][-1] == {"role": "assistant", "content": answer}
    assert row["checkpoint"]["results"][report.result_id] == report.model_dump()
    if resume_terminal:
        # Simulate losing the outer response save after the terminal checkpoint.
        row["response"] = None
    again = await runtime.run(SCOPE, services.history[-1]["content"])
    assert again["response"] == answer and len(calls) == 1
    assert not services.calls and store.writes == 0


@pytest.mark.asyncio
async def test_text_with_tool_call_is_preserved_but_not_delivered_as_final():
    services, store, report, _ = followup_fixture()
    calls = 0

    async def model(messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            result = reply(("read_result", {"result_id": report.result_id}))
            result["content"].insert(0, {"type": "text", "text": "我核对一下适用条件。"})
            return result
        assert messages[-2]["content"] == "我核对一下适用条件。"
        assert outputs(messages)[-1]["result_id"] == report.result_id
        return text_reply("已核对，不能把未知的非科研标准也认定为相同。")

    output = await Supervisor(model, services, store, CONFIG).run(SCOPE, services.history[-1]["content"])
    assert output["response"].startswith("已核对") and calls == 2
    assert "我核对一下" not in output["response"]
    checkpoint = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]
    assert checkpoint["main"]["messages"][1]["content"] == "我核对一下适用条件。"


@pytest.mark.asyncio
async def test_blank_main_response_retries_instead_of_completing():
    services, store, _, _ = followup_fixture()
    calls = 0

    async def model(messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return text_reply("  \n ")
        assert "请回答用户" in messages[0]["content"]
        assert sum(m["role"] == "user" for m in messages) == 2
        return text_reply("现有资料还不能确认全部相同。")

    output = await Supervisor(model, services, store, CONFIG).run(SCOPE, services.history[-1]["content"])
    assert output["outcome"] == "completed" and calls == 2


@pytest.mark.asyncio
async def test_child_prose_does_not_replace_structured_report():
    services = FakeServices()
    store = FakeStore(services)
    child_calls = 0

    async def model(messages, tools, tool_choice):
        nonlocal child_calls
        names = {t["function"]["name"] for t in tools}
        if "delegate" in names:
            assert tool_choice == "required"
            if not outputs(messages):
                return reply(("delegate", {"role": "trip_context", "task": "整理行程"}))
            assert outputs(messages)[-1]["status"] == "needs_input"
            return text_reply("请补充出发城市和日期。")
        child_calls += 1
        assert names == {"report"} and tool_choice == "report"
        if child_calls == 1:
            return text_reply("不能将这段文字当成结构化报告。")
        return reply(("report", {"status": "needs_input", "summary": "需要行程信息",
                                  "data": {"trip": {}, "field_sources": {}}, "missing_info": ["出发地"]}))

    output = await Supervisor(model, services, store, CONFIG).run(SCOPE, "帮我整理一下这次的安排")
    assert output["response"] == "请补充出发城市和日期。" and child_calls == 2
    assert store.writes == 0


@pytest.mark.asyncio
async def test_compaction_changes_model_view_but_preserves_checkpoint_evidence():
    services = FakeServices()
    store = FakeStore(services)
    text = "解释一下刚才的材料"
    await store.call("begin", SCOPE, fingerprint({"text": text, "user_text": text}))
    messages = [{"role": "system", "content": "旧规则"}, {"role": "user", "content": json.dumps({
        "facts": {}, "work": [], "conversation": [{"role": "user", "content": text}]})}]
    for i in range(5):
        call = {"id": f"read-{i}", "name": "read_source", "arguments": {"source_id": f"src_{i}"}}
        messages += [assistant_message([call]), tool_message(call, {
            "id": f"src_{i}", "kind": "policy", "data": {"content": "已保存证据" * 1800}})]
    original = deepcopy(messages[2:])
    store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"] = {
        "trip": {}, "version": trip_version({}), "results": {}, "work_items": {}, "children": {},
        "calls": 0, "preferences_updated": False, "applied_results": [], "discarded_results": [],
        "main": {"messages": messages, "round": 5}}

    async def model(messages, **kwargs):
        observations = outputs(messages)
        assert any("notice" in item and "data" not in item for item in observations)
        assert observations[-1]["data"]["content"] == "已保存证据" * 1800
        return text_reply("已根据现有材料说明。")

    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, text)
    assert result["response"] == "已根据现有材料说明。"
    saved = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]["main"]["messages"]
    assert saved[1:-1] == original


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_text,expected", [(True, "auto"), (False, "report")])
async def test_single_tool_selection_respects_main_vs_child_contract(allow_text, expected):
    async def model(messages, **kwargs):
        assert kwargs["tool_choice"] == expected
        return text_reply("测试")
    assert (await call_model(model, [], [{"function": {"name": "report"}}], allow_text=allow_text)).text == "测试"


@pytest.mark.skipif(os.getenv("HOMMEY_RUN_LIVE_AGENT_TESTS") != "1", reason="opt-in real model evaluation")
@pytest.mark.asyncio
async def test_live_followup_uses_delivered_evidence_without_rereading():
    from agent_runtime.model_client import create_tool_model
    from runtime import _generate_kwargs
    from settings import LLM_CONFIG
    services, store, _, previous = followup_fixture()
    real_model = create_tool_model(LLM_CONFIG, _generate_kwargs(LLM_CONFIG), 30)
    calls = 0

    async def model(messages, **kwargs):
        nonlocal calls
        calls += 1
        return await real_model(messages, **kwargs)

    output = await Supervisor(model, services, store, {**CONFIG, "turn_timeout_sec": 45}).run(
        SCOPE, services.history[-1]["content"])
    checkpoint = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]
    trace = [c["function"]["name"] for m in checkpoint["main"]["messages"] for c in m.get("tool_calls", [])]
    assert output["outcome"] == "completed", output
    assert calls == 1 and trace == [], trace
    assert output["response"] != previous and output["answer_document"] is None
    assert "科研" in output["response"] and "非科研" in output["response"]
    assert any(word in output["response"] for word in ("未知", "未查", "未明确", "不能确认", "无法确认", "不确定", "不一定")), output
    assert not services.calls and store.writes == 0
