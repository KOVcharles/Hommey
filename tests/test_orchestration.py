"""Tests for the DAG-facing child-agent execution adapter."""

import asyncio
import json

from agents.orchestration_agent import OrchestrationAgent
from agentscope.message import Msg
from core.execution_budget import ExecutionBudget, execution_budget_scope


class _ReplyAgent:
    def __init__(self, name, replies):
        self.name = name
        self.replies = list(replies)
        self.calls = 0

    async def reply(self, _message):
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if isinstance(reply, Exception):
            raise reply
        return Msg(name=self.name, content=json.dumps(reply), role="assistant")


class _CapturingAgent(_ReplyAgent):
    def __init__(self, name, replies):
        super().__init__(name, replies)
        self.last_input = None

    async def reply(self, message):
        self.last_input = json.loads(message.content)
        return await super().reply(message)


async def _execute(orchestrator, agent_name, context=None, **overrides):
    kwargs = {
        "agent_name": agent_name,
        "context": context or {},
        "reason": "test",
        "expected_output": "result",
        "previous_results": [],
        "max_retries": 0,
    }
    kwargs.update(overrides)
    return await orchestrator.execute_task(**kwargs)


def test_unregistered_agent_returns_structured_error():
    result = asyncio.run(_execute(OrchestrationAgent(agent_registry={}), "missing"))

    assert result["status"] == "error"
    assert result["error_code"] == "AGENT_NOT_REGISTERED"
    assert result["attempts"] == 0


def test_transient_agent_failure_retries_only_that_agent_once():
    agent = _ReplyAgent("retrying", [ConnectionError("temporary"), {"answer": "ok"}])
    orchestrator = OrchestrationAgent(agent_registry={"retrying": agent}, memory_manager=None)

    async def run():
        budget = ExecutionBudget(max_agent_calls=8)
        with execution_budget_scope(budget):
            result = await _execute(orchestrator, "retrying", max_retries=1)
        return result, budget

    result, budget = asyncio.run(run())
    assert result["status"] == "success"
    assert result["attempts"] == 2
    assert agent.calls == 2
    assert budget.agent_calls == 2


def test_agent_call_budget_stops_the_next_execution():
    first = _ReplyAgent("first", [{"answer": "ok"}])
    second = _ReplyAgent("second", [{"answer": "should not run"}])
    orchestrator = OrchestrationAgent(
        agent_registry={"first": first, "second": second},
        memory_manager=None,
    )

    async def run():
        budget = ExecutionBudget(max_agent_calls=1)
        with execution_budget_scope(budget):
            first_result = await _execute(orchestrator, "first")
            second_result = await _execute(orchestrator, "second")
        return first_result, second_result

    first_result, second_result = asyncio.run(run())
    assert first_result["status"] == "success"
    assert second_result["error_code"] == "AGENT_CALL_LIMIT_EXCEEDED"
    assert second.calls == 0


def test_attachment_context_bypasses_rewritten_query():
    agent = _CapturingAgent("rag_knowledge", [{"answer": "ok"}])
    orchestrator = OrchestrationAgent(
        agent_registry={"rag_knowledge": agent},
        memory_manager=None,
    )
    context = orchestrator.prepare_context(
        {"rewritten_query": "请总结附件", "intents": [], "key_entities": {}},
        request_context={
            "original_query": "住宿上限是多少",
            "agent_query": "住宿上限是多少\n附件正文：每天 500 元",
            "attachment_sources": [{"filename": "policy.docx"}],
        },
    )

    result = asyncio.run(_execute(orchestrator, "rag_knowledge", context=context))

    assert result["status"] == "success"
    assert agent.last_input["context"]["rewritten_query"] == "请总结附件"
    assert "每天 500 元" in agent.last_input["context"]["agent_query"]
