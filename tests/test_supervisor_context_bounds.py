"""Regressions for empty intake and repeated RAG/memory observations."""
import json

import pytest

from agent_runtime.context_window import compact_tool_history, encoded_size
from agent_runtime.contracts import Report, ToolRejected, PolicyReport, schema
from agent_runtime.engine import MAIN_TOOLS, Supervisor, is_intake_entry, is_direct_policy_query
from agent_runtime.model_client import assistant_message, tool_message
from agent_runtime.services import SourceScope
from agent_runtime.validation import validate_report
from tests.test_supervisor_runtime import CONFIG, SCOPE, FakeServices, FakeStore, outputs, reply


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["出差", "chuchai", "chu chai", "我要出差！", "帮我安排出差"])
async def test_empty_intake_is_zero_model_calls_and_replayable(text):
    async def forbidden(*args, **kwargs):
        raise AssertionError("intake must not invoke a model")
    services = FakeServices()
    store = FakeStore(services)
    runtime = Supervisor(forbidden, services, store, CONFIG)
    result = await runtime.run(SCOPE, text)
    document = result["presentation_document"]
    assert document["type"] == "trip_intake"
    assert len(document["missing_required"]) == 5
    assert document["progress"]["completed"] == 0
    assert "已保存" not in result["response"]
    assert services.calls == [] and store.writes == 0
    assert (await runtime.run(SCOPE, text))["idempotent_replay"]


@pytest.mark.parametrize("text", ["要去重庆出差，看看差旅标准", "取消出差", "上次出差去哪里", "出差帮我写代码", "出差，查天气"])
def test_intake_shortcut_does_not_swallow_other_intents(text):
    assert not is_intake_entry(text)


@pytest.mark.parametrize("text", ["要去重庆出差，看看差旅标准", "我准备去上海出差 查一下公司差旅标准", "查询企业差旅制度"])
def test_clear_policy_queries_have_a_single_specialist_route(text):
    assert is_direct_policy_query(text)


@pytest.mark.parametrize("text", ["要去重庆出差，看看差旅标准，再查天气", "看看差旅标准并帮我规划", "上次出差的标准是什么", "取消出差，看看差旅标准"])
def test_mixed_or_ambiguous_policy_queries_keep_supervisor(text):
    assert not is_direct_policy_query(text)


@pytest.mark.asyncio
async def test_direct_policy_runs_one_specialist_and_replays_without_model():
    calls = []
    async def model(messages, tools, tool_choice):
        names = {t["function"]["name"] for t in tools}
        assert "delegate" not in names
        calls.append(names)
        out = outputs(messages)
        if not out:
            return reply(("search_policy", {"query": "重庆 城市分类"}))
        if len(out) == 1:
            return reply(("read_source", {"source_id": out[0]["sources"][0]["source_id"]}))
        ref = out[1]["id"]
        return reply(("report", {"summary": "住宿需符合预算", "evidence_refs": [ref],
            "data": {"findings": [{"item": "住宿", "conclusion": "住宿需符合企业预算并保留发票", "evidence_refs": [ref]}]}}))
    services = FakeServices()
    runtime = Supervisor(model, services, FakeStore(services), CONFIG)
    result = await runtime.run(SCOPE, "要去重庆出差，看看差旅标准")
    assert len(calls) == 3 and len(result["agents"]) == 1
    assert result["answer_document"]["sections"][0]["items"]
    assert (await runtime.run(SCOPE, "要去重庆出差，看看差旅标准"))["idempotent_replay"]
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_short_evidence_can_be_extracted_directly_and_unknowns_stay_partial():
    calls = []
    async def model(messages, tools, tool_choice):
        calls.append(True)
        out = outputs(messages)
        if not out:
            return reply(("search_policy", {"query": "差旅标准"}))
        entry = out[0]["sources"][0]
        assert entry["already_read"] and entry["evidence"]["data"]["content"]
        ref = entry["source_id"]
        return reply(("report", {"summary": "住宿需符合预算，交通未知", "evidence_refs": [ref], "data": {"findings": [
            {"item": "住宿", "conclusion": "住宿需符合企业预算并保留发票", "evidence_refs": [ref]},
            {"item": "交通标准", "conclusion": "尚未查到", "evidence_refs": []}]}}))
    services = FakeServices()
    store = FakeStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, "看看差旅标准")
    assert len(calls) == 2
    assert result["agents"][0]["status"] == "partial"
    assert "待确认：交通标准" in result["response"]
    assert len(result["answer_document"]["sections"][0]["items"]) == 1
    saved = next(iter(store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]["results"].values()))
    from agent_runtime.contracts import SpecialistResult
    brief = SpecialistResult.model_validate(saved).brief()
    assert brief["data"]["findings"] and "sources" not in brief


@pytest.mark.asyncio
async def test_oversized_tool_batch_is_recoverable_without_executing_it():
    async def model(messages, tools, tool_choice):
        out = outputs(messages)
        if not out:
            return reply(("search_policy", {"query": "差旅标准"}))
        ref = out[0]["sources"][0]["source_id"]
        if len(out) == 1:
            return reply(*[("read_source", {"source_id": ref}) for _ in range(8)])
        assert len(out) == 9 and all("error" in item for item in out[1:])
        return reply(("report", {"summary": "住宿需符合预算", "evidence_refs": [ref], "data": {"findings": [
            {"item": "住宿", "conclusion": "住宿需符合企业预算并保留发票", "evidence_refs": [ref]}]}}))
    services = FakeServices()
    result = await Supervisor(model, services, FakeStore(services), CONFIG).run(SCOPE, "看看差旅标准")
    assert result["agents"][0]["status"] == "success"
    assert len(services.calls) == 1


@pytest.mark.asyncio
async def test_empty_trip_specialist_result_renders_form_even_for_answer():
    async def model(messages, tools, tool_choice):
        if "delegate" in {t["function"]["name"] for t in tools}:
            out = outputs(messages)
            if not out:
                return reply(("delegate", {"role": "trip_context", "task": "整理出差需求"}))
            return reply(("finish", {"result_ids": [out[0]["result_id"]]}))
        return reply(("report", {"summary": "待补充出差信息", "status": "needs_input", "missing_info": ["出发地", "目的地"]}))
    services = FakeServices()
    result = await Supervisor(model, services, FakeStore(services), CONFIG).run(SCOPE, "请帮我整理一下出差需求")
    assert result["presentation_document"]["type"] == "trip_intake"


def test_skill_names_are_discoverable_in_tool_schema():
    tool = next(t for t in MAIN_TOOLS if t["function"]["name"] == "read_skill")
    names = tool["function"]["parameters"]["properties"]["name"]["enum"]
    assert "event-collection" in names and "ask-question" in names


def test_policy_wire_schema_is_inline_and_server_bounds_still_apply():
    from pydantic import ValidationError
    wire = schema("report", "report", PolicyReport)["function"]["parameters"]
    assert "$ref" not in json.dumps(wire) and "$defs" not in wire
    assert {"summary", "data", "evidence_refs"} <= set(wire["required"])
    assert wire["properties"]["data"]["properties"]["findings"]["items"]["type"] == "object"
    with pytest.raises(ValidationError):
        PolicyReport(summary="过长" * 200, data={"findings": []}, evidence_refs=[])


@pytest.mark.asyncio
async def test_buffered_adapter_keeps_raw_arguments_and_rejects_repaired_json(monkeypatch):
    from types import SimpleNamespace
    import agentscope.model
    from agent_runtime.model_client import create_tool_model, call_model

    class Base:
        def __init__(self, **kwargs):
            self.options = kwargs
        def _parse_openai_completion_response(self, *args):
            return SimpleNamespace(content=[{"type": "tool_use", "id": "c", "name": "report", "input": {"summary": "repaired"}}])

    monkeypatch.setattr(agentscope.model, "OpenAIChatModel", Base)
    adapter = create_tool_model({"model_name": "test", "api_key": "test", "base_url": "https://example.invalid"}, {})
    assert adapter.options["stream"] is False
    assert adapter.options["client_kwargs"]["max_retries"] == 0
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[
        SimpleNamespace(id="c", function=SimpleNamespace(arguments='{"summary":"incomplete"'))]))])
    parsed = adapter._parse_openai_completion_response(None, response)
    assert parsed.content[0]["raw_input"] == '{"summary":"incomplete"'
    async def model(*args, **kwargs):
        return parsed
    with pytest.raises(ToolRejected, match="完整 JSON"):
        await call_model(model, [], [])


@pytest.mark.asyncio
async def test_buffered_chat_response_does_not_probe_missing_dunder_attributes():
    from agent_runtime.model_client import call_model
    class Response(dict):
        __getattr__ = dict.__getitem__  # AgentScope ChatResponse behaves this way
    async def model(*args, **kwargs):
        return Response(content=[{"type": "tool_use", "id": "c", "name": "report", "input": {"summary": "ok"}}])
    assert (await call_model(model, [], [])).calls[0]["arguments"]["summary"] == "ok"


def test_retrieval_index_deduplicates_and_does_not_repeat_raw_metadata():
    scope = SourceScope()
    raw = {"content": "重庆住宿标准及例外条款" * 1000,
           "chunk_id": "policy-v2-1", "metadata": {"document_version": "2", "effective_date": "2026-01-01",
           "special_exception": "会议期间须另行审批", "retrieval_text": "RAW_DUPLICATE" * 10000}, "fusion_score": 0.1}
    first = scope.add("policy", raw)
    second = scope.add("policy", {**raw, "fusion_score": 0.2, "retrieval_trace_id": "different"})
    assert first["source_id"] == second["source_id"] and len(scope.sources) == 1
    assert encoded_size(first) < 700
    assert "RAW_DUPLICATE" not in json.dumps(first)
    assert scope.sources[first["source_id"]]["data"] == raw
    pages, offset = [], 0
    while True:
        page = scope.read(first["source_id"], offset=offset, limit=4000)
        assert encoded_size(page) < 5000 and page["partial"]
        pages.append(page["data_fragment"])
        offset = page["next_offset"]
        if offset is None:
            break
    data = json.loads("".join(pages))
    assert data["content"] == raw["content"]
    assert data["metadata"]["special_exception"] == "会议期间须另行审批"
    assert "retrieval_text" not in data["metadata"]


def test_long_memory_pages_are_bounded_and_remain_private():
    scope = SourceScope()
    original = {"kind": "message", "data": {"content": "历史对话" * 20000}}
    ref = scope.add("memory", original)["source_id"]
    page = scope.read(ref)
    assert page["next_offset"] == 4000 and encoded_size(page) < 5000
    assert scope.sources[ref]["data"] == original
    with pytest.raises(ToolRejected):
        SourceScope().read(ref)
    with pytest.raises(ToolRejected):
        scope.read(ref, offset=999999)


def test_partial_reports_cannot_cite_unread_search_excerpts():
    scope = SourceScope()
    ref = scope.add("policy", {"content": "规定"})["source_id"]
    with pytest.raises(ToolRejected):
        validate_report("policy_rag", Report(status="partial", summary="规定", evidence_refs=[ref]), scope, [])


def test_policy_completion_without_actual_findings_is_rejected():
    scope = SourceScope()
    ref = scope.add("policy", {"content": "测试制度"})["source_id"]
    scope.read(ref)
    with pytest.raises(ToolRejected, match="具体标准"):
        validate_report("policy_rag", Report(summary="已查到所有标准", evidence_refs=[ref]), scope, [])


def test_context_compaction_preserves_tool_pairs_and_source_access():
    scope = SourceScope()
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "当前请求和明确约束"}]
    for index in range(24):
        ref = scope.add("memory", {"content": str(index) + "历史内容" * 1200})["source_id"]
        call = {"id": str(index), "name": "read_source", "arguments": {"source_id": ref}}
        messages += [assistant_message([call]), tool_message(call, scope.read(ref))]
    before = encoded_size(messages)
    assert before > 80000
    assert compact_tool_history(messages) > 0
    assert encoded_size(messages) < 32000
    assert messages[1]["content"] == "当前请求和明确约束"
    assert len(messages) == 50
    for i in range(2, len(messages), 2):
        call = messages[i]["tool_calls"][0]
        assert call["id"] == messages[i + 1]["tool_call_id"]
        assert scope.read(json.loads(call["function"]["arguments"])["source_id"])


@pytest.mark.asyncio
async def test_repeated_policy_searches_stay_bounded_and_finish_with_summary():
    class LargeServices(FakeServices):
        async def execute(self, scope, name, args):
            assert name == "search_policy"
            self.calls.append(name)
            return "policy", [{"chunk_id": str(i), "content": "重庆住宿标准及适用条件" * 500,
                               "metadata": {"title": "差旅制度", "retrieval_text": "DUPLICATED_RAW" * 800}} for i in range(5)]

    sizes = []
    async def model(messages, tools, tool_choice):
        sizes.append(encoded_size(messages) + encoded_size(tools))
        out = outputs(messages)
        names = {t["function"]["name"] for t in tools}
        if "delegate" in names:
            assert "DUPLICATED_RAW" not in json.dumps(messages)
            if not out:
                return reply(("delegate", {"role": "policy_rag", "task": "查询重庆出差标准"}))
            assert "data_fragment" not in json.dumps(out)
            return reply(("finish", {"result_ids": [out[0]["result_id"]]}))
        if names == {"report"}:
            ref = next(v["id"] for v in out if "id" in v)
            return reply(("report", {"status": "partial", "summary": "已核实部分制度，完整适用条件待确认。", "evidence_refs": [ref],
                "data": {"findings": [{"item": "住宿标准", "conclusion": "重庆住宿标准及适用条件", "evidence_refs": [ref]}]}}))
        if len(out) == 1:
            return reply(("read_source", {"source_id": out[0]["sources"][0]["source_id"]}))
        return reply(("search_policy", {"query": "重庆差旅标准"}))

    services = LargeServices()
    result = await Supervisor(model, services, FakeStore(services), CONFIG).run(SCOPE, "要去重庆出差，看看差旅标准")
    assert result["answer_document"]["sources"]
    assert "完整适用条件待确认" in result["response"]
    assert "住宿标准：重庆住宿标准及适用条件" in result["response"]
    assert services.calls == ["search_policy"]  # later out-of-phase searches are rejected
    assert max(sizes) < 32000


@pytest.mark.asyncio
async def test_child_local_round_limit_does_not_abort_parent_turn():
    async def model(messages, tools, tool_choice):
        if "delegate" in {t["function"]["name"] for t in tools}:
            out = outputs(messages)
            if not out:
                return reply(("delegate", {"role": "policy_rag", "task": "查询重庆标准"}))
            return reply(("finish", {"result_ids": [out[0]["result_id"]]}))
        return reply(("search_policy", {"query": "重庆标准"}))
    services = FakeServices()
    result = await Supervisor(model, services, FakeStore(services), CONFIG).run(SCOPE, "重庆出差标准")
    assert result["agents"][0]["status"] == "unavailable"
    assert "请缩小" not in result["response"]


@pytest.mark.asyncio
async def test_intake_entry_with_attachment_keeps_agent_path():
    called = []
    async def model(messages, tools, tool_choice):
        called.append(True)
        return reply(("finish", {"kind": "ask", "question": "请确认附件中的出差地点"}))
    services = FakeServices()
    result = await Supervisor(model, services, FakeStore(services), CONFIG).run(SCOPE, "出差\n附件内容：请确认地点", user_text="出差")
    assert called and "附件" in result["response"]
