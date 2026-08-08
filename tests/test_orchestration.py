"""Thin-shell orchestration tests: execution ordering, retries, and request context.

调度/暂停/聚合/abort-halt 语义已由 DAG 管线（MultiIntentPipeline）接管；
``OrchestrationAgent.reply`` 是薄壳——按 agent_schedule 分批执行全部任务、
不做 abort 停机、不聚合状态（详见 tests/test_task_orchestration_v2.py）。
"""

import json
import asyncio

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


def test_orchestration_returns_no_agents_for_empty_schedule():
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=None)

    result = asyncio.run(
        orchestrator.reply(
            Msg(
                name="intention",
                content=json.dumps({"agent_schedule": []}),
                role="assistant",
            )
        )
    )

    payload = json.loads(result.content)
    assert payload["status"] == "no_agents"


def test_orchestration_rejects_invalid_intention_json():
    orchestrator = OrchestrationAgent(agent_registry={}, memory_manager=None)

    result = asyncio.run(
        orchestrator.reply(
            Msg(name="intention", content="not-json", role="assistant")
        )
    )

    payload = json.loads(result.content)
    assert payload["error"] == "Invalid intention format"


def test_thin_shell_executes_every_batch_without_abort_halt():
    # 薄壳不做 abort 停机：on_failure=abort 只随结果返回，下游仍执行。
    # 真正的 abort-halt 语义在 DAG 管线中（见 test_task_orchestration_v2.py）。
    required = _ReplyAgent("required", [{"error": "invalid input"}])
    downstream = _ReplyAgent("downstream", [{"answer": "should run"}])
    orchestrator = OrchestrationAgent(
        agent_registry={"required": required, "downstream": downstream},
        memory_manager=None,
    )

    response = asyncio.run(
        orchestrator.reply(
            Msg(
                name="intention",
                content=json.dumps(
                    {
                        "agent_schedule": [
                            {"agent_name": "required", "priority": 1, "on_failure": "abort"},
                            {"agent_name": "downstream", "priority": 2, "on_failure": "abort"},
                        ]
                    }
                ),
                role="assistant",
            )
        )
    )

    payload = json.loads(response.content)
    assert payload["status"] == "completed"
    assert [item["result"]["status"] for item in payload["results"]] == ["error", "success"]
    assert downstream.calls == 1


def test_thin_shell_returns_error_and_success_results_together():
    optional = _ReplyAgent("optional", [{"error": "service unavailable"}])
    required = _ReplyAgent("required", [{"answer": "usable result"}])
    orchestrator = OrchestrationAgent(
        agent_registry={"optional": optional, "required": required},
        memory_manager=None,
    )

    response = asyncio.run(
        orchestrator.reply(
            Msg(
                name="intention",
                content=json.dumps(
                    {
                        "agent_schedule": [
                            {"agent_name": "optional", "priority": 1, "on_failure": "continue"},
                            {"agent_name": "required", "priority": 2, "on_failure": "abort"},
                        ]
                    }
                ),
                role="assistant",
            )
        )
    )

    payload = json.loads(response.content)
    assert payload["status"] == "completed"
    assert [item["result"]["status"] for item in payload["results"]] == ["error", "success"]
    assert required.calls == 1


def test_transient_agent_failure_retries_only_that_agent_once():
    agent = _ReplyAgent("retrying", [ConnectionError("temporary"), {"answer": "ok"}])
    orchestrator = OrchestrationAgent(agent_registry={"retrying": agent}, memory_manager=None)

    async def run():
        budget = ExecutionBudget(max_agent_calls=8)
        with execution_budget_scope(budget):
            response = await orchestrator.reply(
                Msg(
                    name="intention",
                    content=json.dumps(
                        {
                            "agent_schedule": [
                                {
                                    "agent_name": "retrying",
                                    "priority": 1,
                                    "on_failure": "abort",
                                    "max_retries": 1,
                                }
                            ]
                        }
                    ),
                    role="assistant",
                )
            )
        return response, budget

    response, budget = asyncio.run(run())
    payload = json.loads(response.content)
    assert payload["status"] == "completed"
    assert payload["results"][0]["result"]["attempts"] == 2
    assert agent.calls == 2
    assert budget.agent_calls == 2


def test_agent_call_budget_turns_unbounded_execution_into_failure():
    first = _ReplyAgent("first", [{"answer": "ok"}])
    second = _ReplyAgent("second", [{"answer": "should not run"}])
    orchestrator = OrchestrationAgent(
        agent_registry={"first": first, "second": second},
        memory_manager=None,
    )

    async def run():
        budget = ExecutionBudget(max_agent_calls=1)
        with execution_budget_scope(budget):
            return await orchestrator.reply(
                Msg(
                    name="intention",
                    content=json.dumps(
                        {
                            "agent_schedule": [
                                {"agent_name": "first", "priority": 1},
                                {"agent_name": "second", "priority": 2},
                            ]
                        }
                    ),
                    role="assistant",
                )
            )

    payload = json.loads(asyncio.run(run()).content)
    assert payload["status"] == "completed"
    assert payload["results"][1]["result"]["error_code"] == "AGENT_CALL_LIMIT_EXCEEDED"
    assert second.calls == 0


def test_attachment_context_bypasses_rewritten_query():
    agent = _CapturingAgent("rag_knowledge", [{"answer": "ok"}])
    orchestrator = OrchestrationAgent(
        agent_registry={"rag_knowledge": agent},
        memory_manager=None,
    )

    response = asyncio.run(
        orchestrator.reply(
            Msg(
                name="intention",
                content=json.dumps({
                    "rewritten_query": "请总结附件",
                    "agent_schedule": [{"agent_name": "rag_knowledge", "priority": 1}],
                }),
                role="assistant",
            ),
            request_context={
                "original_query": "住宿上限是多少",
                "agent_query": "住宿上限是多少\n附件正文：每天 500 元",
                "attachment_sources": [{"filename": "policy.docx"}],
            },
        )
    )

    assert json.loads(response.content)["status"] == "completed"
    assert agent.last_input["context"]["rewritten_query"] == "请总结附件"
    assert "每天 500 元" in agent.last_input["context"]["agent_query"]
