"""Opt-in real provider/RAG smoke tests; all turn state is isolated in a test store.

Run in the configured application container with HOMMEY_RUN_LIVE_AGENT_TESTS=1.
These tests spend model/query quota but never modify user conversations or trips.
"""
import json
import asyncio
import os
import time

import pytest

from agent_runtime.context_window import encoded_size
from agent_runtime.engine import Supervisor
from agent_runtime.services import BusinessServices
from core.execution_budget import BudgetedModel, ExecutionBudget, execution_budget_scope
from tests.test_supervisor_runtime import FakeServices, FakeStore, SCOPE


pytestmark = pytest.mark.skipif(os.getenv("HOMMEY_RUN_LIVE_AGENT_TESTS") != "1", reason="real provider smoke test is opt-in")


@pytest.mark.asyncio
async def test_live_policy_report_contract():
    from agentscope.model import OpenAIChatModel
    from runtime import _generate_kwargs
    from settings import LLM_CONFIG
    from agent_runtime.contracts import PolicyReport, schema
    from agent_runtime.model_client import call_model, create_tool_model
    model = create_tool_model(LLM_CONFIG, _generate_kwargs(LLM_CONFIG), 30)
    try:
        result = await call_model(model, [{"role": "system", "content": "提取测试制度的明确事实，调用 report，summary 简短。"},
            {"role": "user", "content": "这是隔离的测试证据：重庆属于二类城市，普通员工住宿上限400元/晚，餐费未知。来源 src_policy 已全文回读。只返回城市分类和住宿标准。"}],
            [schema("report", "提取结论和证据", PolicyReport)])
        assert result.calls
        report = PolicyReport.model_validate(result.calls[0]["arguments"])
        assert report.data.findings
    except Exception as exc:
        print(type(exc).__name__, str(exc)[:700], flush=True)
        if isinstance(exc.__cause__, json.JSONDecodeError):
            print("synthetic_report_arguments", exc.__cause__.doc[:1500], flush=True)
        raise


@pytest.mark.asyncio
async def test_live_chongqing_policy_with_bounded_context():
    from agentscope.model import OpenAIChatModel
    from runtime import _generate_kwargs
    from settings import LLM_CONFIG, SUPERVISOR_CONFIG
    from agent_runtime.model_client import create_tool_model
    from collections.abc import AsyncIterable
    raw = BudgetedModel(create_tool_model(LLM_CONFIG, _generate_kwargs(LLM_CONFIG), 60))
    sizes = []

    async def model(messages, **kwargs):
        sizes.append(encoded_size(messages) + encoded_size(kwargs["tools"]))
        print(json.dumps({"model_call": len(sizes), "input_chars": sizes[-1], "tools": [t["function"]["name"] for t in kwargs["tools"]]}), flush=True)
        try:
            response = await raw(messages, **kwargs)
        except Exception as exc:
            print(type(exc).__name__, str(exc)[:700], flush=True)
            raise
        async def capture():
            last = None
            async for chunk in response:
                last = chunk
                yield chunk
            for block in getattr(last, "content", []):
                if block.get("type") == "tool_use":
                    args = block.get("raw_input") or block.get("input")
                    error = None
                    if isinstance(args, str):
                        try:
                            json.loads(args)
                        except json.JSONDecodeError as exc:
                            error = {"message": exc.msg, "position": exc.pos, "near_error": args[max(0, exc.pos-70):exc.pos+70], "tail": args[-70:]}
                    print(json.dumps({"native_tool": block.get("name"), "has_id": bool(block.get("id")), "argument_chars": len(str(args)), "json_error": error}, ensure_ascii=False), flush=True)
        return capture() if isinstance(response, AsyncIterable) else response

    class Services(FakeServices):
        async def execute(self, scope, name, request):
            assert name == "search_policy", "simple policy question must not perform other business queries"
            from utils.io_executor import run_blocking
            return "policy", await run_blocking(policy._policy, request.query)

    policy = object.__new__(BusinessServices)
    policy.retriever = None
    services = Services()
    store = FakeStore(services)
    runtime = Supervisor(model, services, store, SUPERVISOR_CONFIG)
    budget = ExecutionBudget(max_agent_calls=12, max_external_calls=64, max_external_calls_per_type=48)
    started = time.perf_counter()
    try:
        with execution_budget_scope(budget):
            result = await asyncio.wait_for(runtime.run(SCOPE, "要去重庆出差，看看差旅标准"), timeout=120)
    finally:
        checkpoint = store.rows.get((SCOPE.user_id, SCOPE.request_id), {}).get("checkpoint", {})
        print(json.dumps({"trace": [{"tools": [call["function"] for m in state.get("messages", []) for call in m.get("tool_calls", [])],
            "errors": [json.loads(m["content"]) for m in state.get("messages", []) if m["role"] == "tool" and any(k in json.loads(m["content"]) for k in ("error", "status"))],
            "runtime_hints": [m["content"] for m in state.get("messages", [])[2:] if m["role"] == "user"]}
            for state in [checkpoint.get("main", {}), *checkpoint.get("children", {}).values()]]}, ensure_ascii=False))
    elapsed = round(time.perf_counter() - started, 3)
    print(json.dumps({"elapsed_sec": elapsed, "max_input_chars_including_tools": max(sizes),
        "budget": budget.snapshot(), "response": result["response"]}, ensure_ascii=False))
    assert result["answer_document"]["sources"]
    assert any(section["items"] for section in result["answer_document"]["sections"])
    assert "重庆" in result["response"]
    # This deployment's city-classification table explicitly lists Chongqing.
    assert "二类" in result["response"]
    assert "假设重庆" not in result["response"]
    assert "上浮前" not in result["response"]
    assert max(sizes) < 40000
    assert budget.agent_calls == 1
    assert budget.calls_by_type["llm"] <= 5  # includes one report-format repair, never more searches
    assert store.writes == 0
    assert all(agent["status"] not in {"error", "unavailable"} for agent in result["agents"])
