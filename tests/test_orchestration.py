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


def test_required_agent_failure_stops_and_skips_downstream_agents():
    required = _ReplyAgent("required", [{"error": "invalid input"}])
    downstream = _ReplyAgent("downstream", [{"answer": "should not run"}])
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
    assert payload["status"] == "failed"
    assert [item["status"] for item in payload["results"]] == ["error", "skipped"]
    assert downstream.calls == 0


def test_optional_agent_failure_returns_partial_success_and_continues():
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
    assert payload["status"] == "partial_failure"
    assert [item["status"] for item in payload["results"]] == ["error", "success"]
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
    assert payload["results"][0]["attempts"] == 2
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
    assert payload["status"] == "failed"
    assert payload["results"][1]["error_code"] == "AGENT_CALL_LIMIT_EXCEEDED"
    assert second.calls == 0
