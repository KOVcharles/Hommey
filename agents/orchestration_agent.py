"""
OrchestrationAgent 执行层
调度/暂停/聚合/记忆回写等编排语义已由 DAG 管线（MultiIntentPipeline）接管，
本类只保留 DAG 管线需要的执行适配职责：准备共享上下文、执行一个已验证的
任务、登记子智能体以及记录审计结果。它不识别意图，也不创建或维护执行计划。
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from typing import Optional, List, Dict, Any
import json
import logging
import asyncio
import time
import uuid

from core.skill_store import SkillPlatformStore
from core.execution_budget import (
    ExecutionLimitExceeded,
    consume_agent_call,
)
from utils.skill_loader import SkillLoader
from utils.llm_resilience import is_retriable_error

logger = logging.getLogger(__name__)


class OrchestrationAgent(AgentBase):
    """DAG 任务执行适配器；执行计划由 ``MultiIntentPipeline`` 维护。"""

    def __init__(
        self,
        name: str = "OrchestrationAgent",
        agent_registry: Dict[str, AgentBase] = None,
        memory_manager = None,
        skill_store: SkillPlatformStore = None,
        **kwargs
    ):
        """
        初始化协调器

        Args:
            name: 智能体名称
            agent_registry: 子智能体注册表 {agent_name: agent_instance}
            memory_manager: 记忆管理器
        """
        super().__init__()
        self.name = name
        self.agent_registry = agent_registry or {}
        self.memory_manager = memory_manager
        self.skill_store = skill_store or SkillPlatformStore()
        self.skill_definitions = SkillLoader().load_definitions()
        self._agent_skill_map = {
            definition.agent_name: definition.name
            for definition in self.skill_definitions.values()
            if definition.agent_name
        }

    def register_agent(self, agent_name: str, agent: AgentBase):
        """注册子智能体"""
        self.agent_registry[agent_name] = agent
        logger.info(f"Registered agent: {agent_name}")

    def unregister_agent(self, agent_name: str):
        """注销子智能体"""
        if agent_name in self.agent_registry:
            del self.agent_registry[agent_name]
            logger.info(f"Unregistered agent: {agent_name}")

    def prepare_context(
        self,
        intention_data: Dict[str, Any],
        *,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        准备上下文信息，供子智能体使用

        Args:
            intention_data: 意图识别结果

        Returns:
            上下文字典
        """
        request_context = request_context or {}
        # This is an internal staging object, not the prompt context sent to
        # every child agent.  The TaskExecutor selects a minimal, agent-specific
        # projection for each node.  In particular, routing reasoning, the
        # compatibility ``intents`` projection, request-wide entities and the
        # raw user query must not leak into an isolated Goal.
        context = {
            "_agent_runtime": {
                "retrieval_mode": request_context.get("retrieval_mode", "standard"),
                "request_id": request_context.get("request_id", ""),
            },
            "_memory_context": {},
        }

        # 从记忆系统获取上下文
        if self.memory_manager:
            # 短期记忆：最近对话
            recent_context = self.memory_manager.short_term.get_recent_context(3)
            context["_memory_context"]["recent_dialogue"] = recent_context

            # 长期记忆：用户偏好
            preferences = self.memory_manager.long_term.get_preference()
            context["_memory_context"]["user_preferences"] = preferences
            context["_memory_context"]["active_trip"] = self.memory_manager.get_active_trip()

        return context

    # Backward-compatible alias for callers and tests that used the former private helper.
    _prepare_context = prepare_context

    async def execute_task(self, **kwargs) -> Dict[str, Any]:
        """Public runner used by task-scoped orchestration pipelines."""
        return await self._execute_agent(**kwargs)

    def _record_skill_runs(self, intention_data: Dict[str, Any], results: List[Dict]) -> None:
        if not self.skill_store.configured:
            return
        request_id = str(uuid.uuid4())
        user_id = str(getattr(self.memory_manager, "user_id", "unknown"))
        input_summary = {
            "intents": [item.get("type") for item in intention_data.get("intents", [])],
            "entities": intention_data.get("key_entities", {}),
        }
        for result in results:
            agent_name = result.get("agent_name")
            skill_name = self._agent_skill_map.get(agent_name)
            definition = self.skill_definitions.get(skill_name) if skill_name else None
            if not definition:
                continue
            runtime_result = result.get("result") or {}
            data = runtime_result.get("data") if isinstance(runtime_result, dict) else {}
            evidence = []
            if isinstance(data, dict):
                evidence = data.get("retrieved_documents") or data.get("sources") or []
            self.skill_store.record_run(
                request_id=request_id,
                user_id=user_id,
                skill_name=skill_name,
                skill_version=definition.version,
                status=runtime_result.get("status", "unknown"),
                duration_ms=int(float(runtime_result.get("duration_sec") or 0) * 1000),
                input_summary=input_summary,
                output_summary={"agent": agent_name, "status": runtime_result.get("status", "unknown")},
                evidence_count=len(evidence) if isinstance(evidence, list) else 0,
                error_code=(
                    (runtime_result.get("error_code") or "AGENT_ERROR")
                    if runtime_result.get("status") == "error" else None
                ),
            )

    def record_task_results(self, intention_data: Dict[str, Any], task_results: List[Any]) -> None:
        """Record task-pipeline results through the existing skill audit store."""
        legacy_results = []
        for item in task_results:
            legacy_results.append({
                "agent_name": item.agent_name,
                "result": {
                    "status": item.status,
                    "data": item.data,
                    "duration_sec": item.duration_sec,
                    "attempts": item.attempts,
                    "error_code": item.error_code,
                },
            })
        self._record_skill_runs(intention_data, legacy_results)

    async def _execute_agent(
        self,
        agent_name: str,
        context: Dict[str, Any],
        reason: str,
        expected_output: str,
        previous_results: List[Dict],
        task_params: Dict[str, Any] = None,
        max_retries: int = 0,
    ) -> Dict[str, Any]:
        """
        执行单个智能体

        Args:
            agent_name: 智能体名称
            context: 上下文信息
            reason: 调用原因
            expected_output: 期望输出
            previous_results: 前序智能体的结果
            task_params: 任务特定参数（如 MCP 工具的 server_name, tool_name 等）

        Returns:
            执行结果
        """
        # ``task_params.query`` is the final Goal-scope authority.  TaskExecutor
        # has already built a minimal node context.  Keep the one compatibility
        # alias that existing skills consume, but do not restore the old four
        # request/query aliases: they add prompt tokens and invite scope bleed.
        context = dict(context)
        scoped_query = str((task_params or {}).get("query") or "").strip()
        if scoped_query:
            context["agent_query"] = scoped_query

        # 检查智能体是否注册
        if agent_name not in self.agent_registry:
            logger.warning(f"Agent not registered: {agent_name}")
            return {
                "status": "error",
                "agent_name": agent_name,
                "data": {},
                "error_code": "AGENT_NOT_REGISTERED",
                "error_message": f"智能体未注册: {agent_name}",
                "retryable": False,
                "attempts": 0,
            }

        try:
            agent = self.agent_registry[agent_name]
        except Exception as exc:
            logger.error("Agent load failed: %s, error: %s", agent_name, exc)
            return self._error_result(
                agent_name,
                exc,
                duration_sec=0.0,
                attempts=0,
                retryable=False,
                error_code="AGENT_LOAD_FAILED",
            )

        # 构建输入消息（包含 task_params）
        msg_data = {
            "context": context,
            "reason": reason,
            "expected_output": expected_output,
            "previous_results": previous_results,
        }
        if task_params:
            msg_data["task_params"] = task_params

        input_msg = Msg(
            name="Orchestrator",
            content=json.dumps(msg_data, ensure_ascii=False),
            role="user"
        )

        start_time = time.perf_counter()
        retries = max(0, min(int(max_retries or 0), 2))
        for attempt in range(retries + 1):
            attempts = attempt + 1
            try:
                consume_agent_call(agent_name)
                response = await agent.reply(input_msg)
                payload = self._parse_agent_response(response)
                result = self._normalize_agent_payload(agent_name, payload)
                result["attempts"] = attempts
                result["duration_sec"] = time.perf_counter() - start_time
                if result["status"] == "success" or not result.get("retryable") or attempt >= retries:
                    logger.info(
                        "Agent %s finished status=%s attempts=%d in %.3fs",
                        agent_name,
                        result["status"],
                        attempts,
                        result["duration_sec"],
                    )
                    return result
            except ExecutionLimitExceeded as exc:
                return self._error_result(
                    agent_name,
                    exc,
                    duration_sec=time.perf_counter() - start_time,
                    attempts=attempts,
                    retryable=False,
                    error_code=exc.code,
                )
            except Exception as exc:
                retryable = is_retriable_error(exc)
                if not retryable or attempt >= retries:
                    logger.error("Agent execution failed: %s, error: %s", agent_name, exc)
                    return self._error_result(
                        agent_name,
                        exc,
                        duration_sec=time.perf_counter() - start_time,
                        attempts=attempts,
                        retryable=retryable,
                    )

            await asyncio.sleep(0.5 * (2 ** attempt))

        return self._error_result(
            agent_name,
            RuntimeError("Agent execution exhausted without a result"),
            duration_sec=time.perf_counter() - start_time,
            attempts=retries + 1,
            retryable=False,
        )

    @staticmethod
    def _parse_agent_response(response) -> Any:
        content = getattr(response, "content", response)
        if isinstance(content, str):
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return {"output": content}
        return content

    @staticmethod
    def _normalize_agent_payload(agent_name: str, payload: Any) -> Dict[str, Any]:
        data = payload if isinstance(payload, dict) else {"output": payload}
        status = str(data.get("status") or "").lower()
        error_message = data.get("error")
        if status == "error" and not error_message:
            error_message = data.get("message") or "Agent returned error status"

        nested_results = data.get("results") if isinstance(data.get("results"), dict) else {}
        if data.get("query_success") is False and nested_results.get("error"):
            error_message = nested_results["error"]

        if error_message:
            retryable = bool(data.get("retryable")) or is_retriable_error(
                RuntimeError(str(error_message))
            )
            return {
                "status": "error",
                "agent_name": agent_name,
                "data": data,
                "error_code": str(data.get("error_code") or "AGENT_EXECUTION_FAILED"),
                "error_message": str(error_message),
                "retryable": retryable,
            }

        return {
            "status": "success",
            "agent_name": agent_name,
            "data": data,
            "error_code": None,
            "error_message": None,
            "retryable": False,
        }

    @staticmethod
    def _error_result(
        agent_name: str,
        exc: Exception,
        *,
        duration_sec: float,
        attempts: int,
        retryable: bool,
        error_code: str = "AGENT_EXECUTION_FAILED",
    ) -> Dict[str, Any]:
        public_message = getattr(exc, "public_message", None) or "Agent 执行失败"
        return {
            "status": "error",
            "agent_name": agent_name,
            "duration_sec": duration_sec,
            "attempts": attempts,
            "data": {"error": str(exc)},
            "error_code": error_code,
            "error_message": public_message,
            "retryable": retryable,
        }
