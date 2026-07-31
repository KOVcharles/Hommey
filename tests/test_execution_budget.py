import asyncio

import pytest

from core.execution_budget import (
    BudgetedModel,
    ExecutionBudget,
    ExecutionLimitExceeded,
    consume_external_call,
    execution_budget_scope,
)
from settings import RESILIENCE_CONFIG
from webui_new.core.errors import UpstreamError
from webui_new.manager import HommeyWebInstance


@pytest.fixture
def anyio_backend():
    """被测请求超时逻辑基于 asyncio.wait_for。"""
    return "asyncio"


def test_execution_budget_enforces_total_and_per_type_limits():
    budget = ExecutionBudget(
        max_agent_calls=2,
        max_external_calls=2,
        max_external_calls_per_type=1,
    )

    with execution_budget_scope(budget):
        consume_external_call("llm")
        with pytest.raises(ExecutionLimitExceeded) as per_type:
            consume_external_call("llm")
        assert per_type.value.code == "EXTERNAL_CALL_TYPE_LIMIT_EXCEEDED"

        consume_external_call("rag")
        with pytest.raises(ExecutionLimitExceeded) as total:
            consume_external_call("weather")
        assert total.value.code == "EXTERNAL_CALL_LIMIT_EXCEEDED"

    assert budget.snapshot()["calls_by_type"] == {"llm": 1, "rag": 1}


def test_execution_budget_scope_is_request_local():
    first = ExecutionBudget(max_external_calls=1)
    second = ExecutionBudget(max_external_calls=1)

    with execution_budget_scope(first):
        consume_external_call("llm")
    with execution_budget_scope(second):
        consume_external_call("llm")

    assert first.external_calls == 1
    assert second.external_calls == 1


def test_budgeted_model_counts_each_model_invocation():
    class Model:
        async def __call__(self, messages):
            return messages[-1]["content"]

    async def run():
        budget = ExecutionBudget(max_external_calls=2, max_external_calls_per_type=2)
        with execution_budget_scope(budget):
            model = BudgetedModel(Model())
            assert await model([{"role": "user", "content": "hello"}]) == "hello"
            assert await model([{"role": "user", "content": "again"}]) == "again"
        return budget

    budget = asyncio.run(run())
    assert budget.snapshot()["calls_by_type"] == {"llm": 2}


@pytest.mark.anyio
async def test_request_timeout_is_converted_to_public_error(monkeypatch):
    instance = HommeyWebInstance("u1")

    async def slow_request(_message, request_id=None, attachment_ids=None):
        await asyncio.sleep(0.05)
        return {}

    monkeypatch.setattr(instance, "_process_message_impl", slow_request)
    monkeypatch.setitem(RESILIENCE_CONFIG, "request_timeout_sec", 0.001)

    with pytest.raises(UpstreamError) as exc:
        await instance.process_message("slow")

    assert exc.value.code == "REQUEST_EXECUTION_TIMEOUT"
    assert exc.value.retryable is True


@pytest.mark.anyio
async def test_request_budget_error_is_not_retryable(monkeypatch):
    instance = HommeyWebInstance("u1")

    async def exhaust_budget(_message, request_id=None, attachment_ids=None):
        for _ in range(17):
            consume_external_call("weather")
        return {}

    monkeypatch.setattr(instance, "_process_message_impl", exhaust_budget)

    with pytest.raises(UpstreamError) as exc:
        await instance.process_message("loop")

    assert exc.value.code == "EXTERNAL_CALL_TYPE_LIMIT_EXCEEDED"
    assert exc.value.retryable is False
