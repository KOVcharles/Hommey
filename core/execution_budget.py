"""Per-request execution budgets for agents and outbound operations."""
from __future__ import annotations

import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Iterator, Optional


class ExecutionLimitExceeded(RuntimeError):
    """Raised when a request exhausts one of its execution budgets."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.public_message = message
        super().__init__(message)


@dataclass
class ExecutionBudget:
    """Mutable counters shared by every operation in one async request."""

    max_agent_calls: int = 8
    max_external_calls: int = 16
    max_external_calls_per_type: int = 6
    agent_calls: int = 0
    external_calls: int = 0
    calls_by_type: dict[str, int] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def consume_agent(self, agent_name: str) -> None:
        with self._lock:
            if self.agent_calls >= self.max_agent_calls:
                raise ExecutionLimitExceeded(
                    "AGENT_CALL_LIMIT_EXCEEDED",
                    "本次请求的 Agent 调用次数已达到安全上限",
                )
            self.agent_calls += 1

    def consume_external(self, call_type: str) -> None:
        normalized_type = str(call_type or "unknown").strip().lower() or "unknown"
        with self._lock:
            current_type_calls = self.calls_by_type.get(normalized_type, 0)
            if self.external_calls >= self.max_external_calls:
                raise ExecutionLimitExceeded(
                    "EXTERNAL_CALL_LIMIT_EXCEEDED",
                    "本次请求的外部调用总次数已达到安全上限",
                )
            if current_type_calls >= self.max_external_calls_per_type:
                raise ExecutionLimitExceeded(
                    "EXTERNAL_CALL_TYPE_LIMIT_EXCEEDED",
                    f"本次请求的 {normalized_type} 调用次数已达到安全上限",
                )
            self.external_calls += 1
            self.calls_by_type[normalized_type] = current_type_calls + 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "agent_calls": self.agent_calls,
                "max_agent_calls": self.max_agent_calls,
                "external_calls": self.external_calls,
                "max_external_calls": self.max_external_calls,
                "max_external_calls_per_type": self.max_external_calls_per_type,
                "calls_by_type": dict(self.calls_by_type),
            }


_CURRENT_BUDGET: ContextVar[Optional[ExecutionBudget]] = ContextVar(
    "hommey_execution_budget",
    default=None,
)


def current_execution_budget() -> Optional[ExecutionBudget]:
    return _CURRENT_BUDGET.get()


@contextmanager
def execution_budget_scope(budget: ExecutionBudget) -> Iterator[ExecutionBudget]:
    token = _CURRENT_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _CURRENT_BUDGET.reset(token)


def consume_agent_call(agent_name: str) -> None:
    budget = current_execution_budget()
    if budget is not None:
        budget.consume_agent(agent_name)


def consume_external_call(call_type: str) -> None:
    budget = current_execution_budget()
    if budget is not None:
        budget.consume_external(call_type)


class BudgetedModel:
    """Transparent model proxy that counts every LLM invocation once."""

    def __init__(self, model: Any):
        self._model = model

    async def __call__(self, *args, **kwargs):
        consume_external_call("llm")
        result = self._model(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)
