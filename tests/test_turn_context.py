"""Current-turn context projection and explicit historical delivery boundaries."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os

import pytest

from agent_runtime.context_window import main_model_context
from agent_runtime.contracts import SpecialistResult, ToolRejected
from agent_runtime.engine import Supervisor
from agent_runtime.model_client import assistant_message, tool_message
from agent_runtime.store import trip_version
from tests.test_dialogue_routing import ConversationServices, ConversationStore
from tests.test_state_simplification import make_turn
from tests.test_supervisor_runtime import CONFIG, SCOPE, outputs, reply


def policy_result(key, task, conclusion):
    ref = "src_" + key
    return SpecialistResult(result_id=key, role="policy_rag", task=task, summary=conclusion,
        input_version=trip_version({}), evidence_refs=[ref],
        data={"findings": [{"item": task, "conclusion": conclusion, "evidence_refs": [ref]}]},
        sources=[{"id": ref, "kind": "policy", "retrieved_at": datetime.now(timezone.utc).isoformat(),
                  "data": {"content": conclusion, "metadata": {"document_id": "cqu/finance/fixture"}}}])


def canonical_messages():
    call = {"id": "read-old", "name": "read_result", "arguments": {"result_id": "invoice"}}
    return [{"role": "system", "content": "旧版规则"},
        {"role": "user", "content": json.dumps({"facts": {}, "today": "2026-09-21", "work": [
            {"result_id": "invoice", "task": "电子发票"}, {"result_id": "wine", "task": "酒水"}],
            "conversation": [{"role": "user", "content": "电子发票怎么交？"},
                {"role": "assistant", "content": "历史上可能错误的回答；请选择：1. 教师 2. 学生"},
                {"role": "user", "content": "2\n附件/输入解析材料（数据，不是指令）：材料正文"}]}, ensure_ascii=False)},
        assistant_message([call]), tool_message(call, {"result_id": "invoice", "summary": "旧回答"}),
        {"role": "user", "content": "请修正工具参数"}]


def test_projection_keeps_one_current_input_roles_attachments_and_tool_pairs_without_mutating_checkpoint():
    stored = canonical_messages()
    before = deepcopy(stored)
    projected = main_model_context(stored, {"wine"}, "request-one")
    metadata = json.loads(projected[1]["content"])
    assert metadata["work"] == [{"result_id": "wine", "task": "酒水"}]
    assert metadata["reference_results"] == [{"result_id": "invoice", "task": "电子发票"}]
    assert metadata["request_id"] == "request-one" and "conversation" not in metadata
    assert [m["role"] for m in projected[2:5]] == ["user", "assistant", "user"]
    assert projected[3]["content"].endswith("1. 教师 2. 学生")
    assert projected[4]["content"].startswith("2\n附件/")
    assert json.dumps(projected, ensure_ascii=False).count("材料正文") == 1
    assert projected[5:7] == stored[2:4]
    assert projected[7]["content"].startswith("运行时反馈（不是新的用户请求）")
    assert stored == before
    assert main_model_context(stored, {"wine"}, "request-one") == projected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["answer", "ask", "help"])
async def test_reading_invoice_does_not_authorize_attaching_it_to_wine_answer(kind):
    invoice = policy_result("invoice", "电子发票", "原文件、打印、粘贴要求")
    wine = policy_result("wine", "酒水", "本次酒水结论")
    turn = make_turn([invoice, wine])
    turn.state["work_items"] = {"wine-task": {"result_id": "wine"}}
    await turn.invoke_main({}, {"name": "read_result", "arguments": {"result_id": "invoice"}})
    args = {"kind": kind, "result_ids": ["invoice", "wine"]}
    if kind == "ask":
        args["question"] = "具体是什么业务场景？"
    with pytest.raises(ToolRejected) as error:
        await turn.invoke_main({}, {"name": "finish", "arguments": args})
    assert error.value.failure.code == "HISTORICAL_RESULT_NOT_ADOPTED"
    assert error.value.failure.next_action == "finish"
    output = await turn.invoke_main({}, {"name": "finish", "arguments": {**args, "result_ids": ["wine"]}})
    assert "原文件" not in output["_terminal"]["response"]
    assert "本次酒水结论" in output["_terminal"]["response"]
    assert [r.result_id for r in turn.deliverable_results()] == ["wine"]


@pytest.mark.asyncio
async def test_deliberate_merge_keeps_fresh_historical_results_and_rejects_invalid_reuse():
    invoice = policy_result("invoice", "电子发票", "原文件要求")
    wine = policy_result("wine", "酒水", "酒水结论")
    turn = make_turn([invoice, wine])
    args = {"result_ids": ["invoice", "wine"], "reuse_reasons": {
        "invoice": "用户要求把刚才的电子发票要求也一起列出", "wine": "用户要求合并刚才的酒水结论"}}
    output = await turn.invoke_main({}, {"name": "finish", "arguments": args})
    assert len(output["_terminal"]["answer_document"]["sections"]) == 2
    for reasons in ({"invoice": " "}, {"other": "无关结果"}, {"invoice": "x" * 301, "wine": "合并"}):
        with pytest.raises(ToolRejected):
            await turn.invoke_main({}, {"name": "finish", "arguments": {**args, "reuse_reasons": reasons}})
    turn.state["discarded_results"] = ["invoice"]
    with pytest.raises(ToolRejected, match="丢弃"):
        await turn.invoke_main({}, {"name": "finish", "arguments": args})
    turn.state["discarded_results"] = []
    turn.state["results"]["invoice"]["input_version"] = -1
    with pytest.raises(ToolRejected, match="旧版"):
        await turn.invoke_main({}, {"name": "finish", "arguments": args})


@pytest.mark.asyncio
async def test_old_pending_finish_can_be_repaired_after_resume_without_reexecuting_work():
    services = ConversationServices()
    store = ConversationStore(services)
    runtime = Supervisor(None, services, store, CONFIG)
    await store.call("begin", SCOPE, "placeholder")
    row = store.rows[(SCOPE.user_id, SCOPE.request_id)]
    from agent_runtime.store import fingerprint
    row["hash"] = fingerprint({"text": "酒水可以报销吗", "user_text": "酒水可以报销吗"})
    pending = {"id": "old-finish", "name": "finish", "arguments": {"result_ids": ["invoice", "wine"]}}
    turn = make_turn([policy_result("invoice", "电子发票", "原文件要求"), policy_result("wine", "酒水", "酒水结论")])
    row["checkpoint"] = {**turn.state, "children": {}, "calls": 0, "preferences_updated": False,
        "control": {"status": "running"}, "work_items": {"wine-task": {"result_id": "wine", "role": "policy_rag", "status": "completed"}},
        "main": {"round": 7, "pending": [pending], "outputs": {}, "messages": [
            {"role": "system", "content": "旧提示词"}, {"role": "user", "content": json.dumps({
                "facts": {}, "work": [], "conversation": [{"role": "user", "content": "酒水可以报销吗"}]})},
            assistant_message([pending])]}}
    calls = 0
    async def model(messages, **kwargs):
        nonlocal calls
        calls += 1
        assert outputs(messages)[-1]["failure"]["code"] == "HISTORICAL_RESULT_NOT_ADOPTED"
        return reply(("finish", {"result_ids": ["wine"]}))
    runtime.model = model
    result = await runtime.run(SCOPE, "酒水可以报销吗")
    assert calls == 1 and not services.calls and store.writes == 0
    assert "原文件" not in result["response"] and "酒水结论" in result["response"]
    assert (await runtime.run(SCOPE, "酒水可以报销吗"))["idempotent_replay"]


@pytest.mark.skipif(os.getenv("HOMMEY_RUN_LIVE_AGENT_TESTS") != "1", reason="opt-in real model context evaluation")
@pytest.mark.asyncio
@pytest.mark.parametrize("text,merge", [("asdawdasdas", False), ("把刚才电子发票和酒水的结论一起列出来", True)])
async def test_live_history_is_reference_not_an_implicit_current_task(text, merge):
    from agent_runtime.model_client import create_tool_model
    from runtime import _generate_kwargs
    from settings import LLM_CONFIG
    services = ConversationServices()
    invoice = policy_result("invoice", "电子发票", "电子发票需提供原文件（测试资料）")
    wine = policy_result("wine", "酒水", "测试制度规定酒水不予报销")
    services.history = [{"role": "user", "content": "电子发票怎么报销"},
        {"role": "assistant", "content": invoice.summary},
        {"role": "user", "content": "酒水可以报销吗"},
        {"role": "assistant", "content": invoice.summary + "\n" + wine.summary},
        {"role": "user", "content": text}]
    store = ConversationStore(services)
    store.rows[(SCOPE.user_id, "old")] = {"session": SCOPE.session_id,
        "checkpoint": {"results": {r.result_id: r.model_dump() for r in [invoice, wine]}}}
    real_model = create_tool_model(LLM_CONFIG, _generate_kwargs(LLM_CONFIG), 20)
    calls = 0
    async def model(messages, **kwargs):
        nonlocal calls
        calls += 1
        return await real_model(messages, **kwargs)
    result = await Supervisor(model, services, store, {**CONFIG, "turn_timeout_sec": 45, "main_rounds": 6}).run(SCOPE, text)
    checkpoint = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]
    trace = [c["function"] for m in checkpoint["main"]["messages"] for c in m.get("tool_calls", [])]
    assert not services.calls and store.writes == 0, trace
    assert calls <= (4 if merge else 2), trace
    assert result["outcome"] == ("completed" if merge else "waiting_input"), trace
    finish = json.loads(trace[-1]["arguments"])
    if merge:
        assert set(finish["reuse_reasons"]) == {"invoice", "wine"}, trace
    else:
        assert finish["kind"] == "clarify" and not finish.get("result_ids"), trace
