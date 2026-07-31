"""
协调器智能体 OrchestrationAgent
职责：根据意图识别结果，协调调度多个子智能体完成任务

核心功能：
1. 接收 IntentionAgent 的调度决策
2. 按照优先级顺序执行子智能体
3. 管理智能体之间的消息传递
4. 聚合多个智能体的结果
5. 与三层记忆系统集成

执行模式：
- Sequential (顺序执行): 按优先级依次执行，前一个的输出作为后一个的输入
- Parallel (并行执行): 同时执行多个智能体（暂不实现）
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from typing import Optional, Union, List, Dict, Any
import json
import logging
import asyncio
import time
import uuid

from core.skill_store import SkillPlatformStore
from core.execution_budget import (
    ExecutionBudget,
    ExecutionLimitExceeded,
    consume_agent_call,
    current_execution_budget,
    execution_budget_scope,
)
from settings import RESILIENCE_CONFIG
from utils.skill_loader import SkillLoader
from utils.llm_resilience import is_retriable_error
from utils.memory_safety import filter_safe_memory_mapping, is_safe_preference_value

logger = logging.getLogger(__name__)


class OrchestrationAgent(AgentBase):
    """协调器智能体 - 调度和协调多个子智能体"""

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

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        """Execute with the caller's budget, or create one for non-Web entrypoints."""
        if current_execution_budget() is not None:
            return await self._reply_impl(x)

        rc = RESILIENCE_CONFIG
        budget = ExecutionBudget(
            max_agent_calls=rc.get("max_agent_calls_per_request", 8),
            max_external_calls=rc.get("max_external_calls_per_request", 16),
            max_external_calls_per_type=rc.get("max_external_calls_per_type", 6),
        )
        try:
            with execution_budget_scope(budget):
                return await asyncio.wait_for(
                    self._reply_impl(x),
                    timeout=rc.get("request_timeout_sec", 120.0),
                )
        finally:
            logger.info("Orchestration execution budget: %s", budget.snapshot())

    async def _reply_impl(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        """
        协调执行流程

        Args:
            x: 输入消息，应包含 IntentionAgent 的输出

        Returns:
            Msg: 执行结果
        """
        if x is None:
            return Msg(
                name=self.name,
                content=json.dumps({"error": "No input provided"}),
                role="assistant"
            )

        # 解析输入
        if isinstance(x, list):
            intention_output = x[-1].content if x else "{}"
        else:
            intention_output = x.content

        # 解析意图识别结果
        try:
            intention_data = json.loads(intention_output) if isinstance(intention_output, str) else intention_output
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse intention output: {e}")
            return Msg(
                name=self.name,
                content=json.dumps({"error": "Invalid intention format"}),
                role="assistant"
            )

        routing = intention_data.get("routing") or {}
        if routing.get("should_call_skill") is False:
            return Msg(
                name=self.name,
                content=json.dumps({
                    "status": "no_agents",
                    "routing": routing,
                    "message": intention_data.get("clarification")
                    or self._message_for_non_skill_intent(routing.get("intent")),
                    "results": [],
                }, ensure_ascii=False),
                role="assistant",
            )

        # 获取智能体调度计划
        agent_schedule = intention_data.get("agent_schedule", [])
        agent_schedule, disabled_skills = self._filter_enabled_schedule(agent_schedule)
        if not agent_schedule:
            return Msg(
                name=self.name,
                content=json.dumps({
                    "status": "no_agents",
                    "message": (
                        f"相关能力当前已停用：{', '.join(disabled_skills)}"
                        if disabled_skills else "没有需要调度的智能体"
                    )
                }, ensure_ascii=False),
                role="assistant"
            )

        # 按优先级排序
        sorted_schedule = sorted(agent_schedule, key=lambda x: x.get("priority", 999))

        logger.info(f"Orchestrating {len(sorted_schedule)} agents")

        # 准备上下文信息
        context = self._prepare_context(intention_data)

        # 按优先级分批执行；同一优先级并行，不同优先级顺序执行。
        results = []
        halted = False
        paused_for_input = False
        priorities = sorted({task.get("priority", 999) for task in sorted_schedule})
        for priority in priorities:
            batch = [task for task in sorted_schedule if task.get("priority", 999) == priority]
            batch_results = await self._execute_parallel_agents(batch, context, results)
            results.extend(batch_results)

            if self._has_abort_failure(batch_results):
                remaining = [
                    task for task in sorted_schedule
                    if task.get("priority", 999) > priority
                ]
                results.extend(self._build_skipped_results(remaining))
                halted = True
                break

            if self._pause_incomplete_trip_planning(sorted_schedule, results):
                paused_for_input = True
                break

        if not halted and not paused_for_input:
            await self._continue_ready_trip_planning(sorted_schedule, context, results)

        # 聚合结果
        final_result = self._aggregate_results(results, intention_data)

        # 更新记忆
        if self.memory_manager:
            successful_results = [
                item for item in results
                if (item.get("result") or {}).get("status") == "success"
            ]
            self._update_memory(intention_data, successful_results)

        self._record_skill_runs(intention_data, results)

        return Msg(
            name=self.name,
            content=json.dumps(final_result, ensure_ascii=False),
            role="assistant"
        )

    @staticmethod
    def _pause_incomplete_trip_planning(schedule: List[Dict], results: List[Dict]) -> bool:
        """Stop a planning workflow after collection until required facts exist."""
        if not any(item.get("agent_name") == "itinerary_planning" for item in schedule):
            return False
        event_result = next(
            (item for item in reversed(results) if item.get("agent_name") == "event_collection"),
            None,
        )
        if not event_result:
            return False
        runtime_result = event_result.get("result") or {}
        data = runtime_result.get("data") if isinstance(runtime_result, dict) else {}
        return isinstance(data, dict) and data.get("planning_ready") is False

    async def _continue_ready_trip_planning(
        self,
        schedule: List[Dict],
        context: Dict[str, Any],
        results: List[Dict],
    ) -> None:
        """Resume an active trip as soon as its final required fact is collected."""
        if any(item.get("agent_name") == "itinerary_planning" for item in schedule):
            return
        event_result = next(
            (item for item in reversed(results) if item.get("agent_name") == "event_collection"),
            None,
        )
        if not event_result:
            return
        data = (event_result.get("result") or {}).get("data") or {}
        if not isinstance(data, dict) or data.get("planning_ready") is not True:
            return

        plan_definition = self.skill_definitions.get("plan-trip")
        if not plan_definition:
            return
        executed_agents = {item.get("agent_name") for item in results}
        follow_up = [
            step.model_dump()
            for step in plan_definition.execution
            if step.agent_name != "event_collection" and step.agent_name not in executed_agents
        ]
        follow_up, _ = self._filter_enabled_schedule(follow_up)
        priorities = sorted({item.get("priority", 999) for item in follow_up})
        for priority in priorities:
            batch = [item for item in follow_up if item.get("priority", 999) == priority]
            batch_results = await self._execute_parallel_agents(batch, context, results)
            results.extend(batch_results)
            if self._has_abort_failure(batch_results):
                remaining = [
                    item for item in follow_up
                    if item.get("priority", 999) > priority
                ]
                results.extend(self._build_skipped_results(remaining))
                return

    @staticmethod
    def _has_abort_failure(results: List[Dict[str, Any]]) -> bool:
        return any(
            (item.get("result") or {}).get("status") == "error"
            and item.get("on_failure", "abort") == "abort"
            for item in results
        )

    @staticmethod
    def _build_skipped_results(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "agent_name": task.get("agent_name"),
                "priority": task.get("priority", 0),
                "on_failure": task.get("on_failure", "abort"),
                "result": {
                    "status": "skipped",
                    "agent_name": task.get("agent_name"),
                    "data": {},
                    "error_code": "UPSTREAM_DEPENDENCY_FAILED",
                    "error_message": "前置关键步骤失败，本步骤未执行",
                    "retryable": False,
                    "attempts": 0,
                },
            }
            for task in tasks
        ]

    def _prepare_context(self, intention_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        准备上下文信息，供子智能体使用

        Args:
            intention_data: 意图识别结果

        Returns:
            上下文字典
        """
        context = {
            "reasoning": intention_data.get("reasoning", ""),
            "intents": intention_data.get("intents", []),
            "key_entities": intention_data.get("key_entities", {}),
            "rewritten_query": intention_data.get("rewritten_query", "")
        }

        # 从记忆系统获取上下文
        if self.memory_manager:
            # 短期记忆：最近对话
            recent_context = self.memory_manager.short_term.get_recent_context(3)
            context["recent_dialogue"] = recent_context

            # 长期记忆：用户偏好
            preferences = self.memory_manager.long_term.get_preference()
            context["user_preferences"] = preferences
            context["active_trip"] = self.memory_manager.get_active_trip()

        return context

    def _filter_enabled_schedule(self, schedule: List[Dict[str, Any]]):
        enabled = []
        disabled = []
        for task in schedule:
            skill_name = self._agent_skill_map.get(task.get("agent_name"))
            definition = self.skill_definitions.get(skill_name) if skill_name else None
            default = definition.enabled_by_default if definition else True
            if skill_name and not self.skill_store.is_enabled(skill_name, default):
                disabled.append(skill_name)
                continue
            enabled.append(task)
        return enabled, disabled

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

    async def _execute_parallel_agents(
        self,
        tasks: List[Dict],
        context: Dict[str, Any],
        previous_results: List[Dict]
    ) -> List[Dict]:
        """
        并行执行多个智能体

        Args:
            tasks: 任务列表，每个任务包含 agent_name, priority, reason, expected_output, params（可选，传递给 Agent 的额外参数）
            context: 上下文信息
            previous_results: 前序智能体的结果

        Returns:
            执行结果列表
        """
        if not tasks:
            return []

        # 如果只有一个任务，直接执行
        if len(tasks) == 1:
            task = tasks[0]
            result = await self._execute_agent(
                agent_name=task.get("agent_name"),
                context=context,
                reason=task.get("reason", ""),
                expected_output=task.get("expected_output", ""),
                previous_results=previous_results,
                task_params=task.get("params", {}),
                max_retries=task.get("max_retries", 0),
            )
            return [{
                "agent_name": task.get("agent_name"),
                "priority": task.get("priority", 0),
                "on_failure": task.get("on_failure", "abort"),
                "result": result
            }]

        # 多个任务并行执行
        logger.info(f"Executing {len(tasks)} agents in parallel")

        # 创建并行任务
        parallel_coroutines = []
        for task in tasks:
            agent_name = task.get("agent_name")
            priority = task.get("priority", 0)
            reason = task.get("reason", "")
            expected_output = task.get("expected_output", "")
            task_params = task.get("params", {})

            logger.info(f"Parallel executing agent: {agent_name} (priority={priority})")

            # 创建协程
            coroutine = self._execute_agent(
                agent_name=agent_name,
                context=context,
                reason=reason,
                expected_output=expected_output,
                previous_results=previous_results,
                task_params=task_params,
                max_retries=task.get("max_retries", 0),
            )
            parallel_coroutines.append((agent_name, priority, task.get("on_failure", "abort"), coroutine))

        # 使用 asyncio.gather 并行执行
        execution_results = await asyncio.gather(
            *[coro for _, _, _, coro in parallel_coroutines],
            return_exceptions=True
        )

        # 整理结果
        results = []
        for (agent_name, priority, on_failure, _), exec_result in zip(parallel_coroutines, execution_results):
            if isinstance(exec_result, Exception):
                logger.error(f"Parallel agent execution failed: {agent_name}, error: {exec_result}")
                result = {
                    "status": "error",
                    "agent_name": agent_name,
                    "data": {"error": str(exec_result)},
                    "error_code": "AGENT_EXECUTION_FAILED",
                    "error_message": "Agent 并行执行失败",
                    "retryable": is_retriable_error(exec_result),
                    "attempts": 1,
                }
            else:
                result = exec_result

            results.append({
                "agent_name": agent_name,
                "priority": priority,
                "on_failure": on_failure,
                "result": result
            })

        return results

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

    def _aggregate_results(
        self,
        results: List[Dict],
        intention_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        聚合多个智能体的结果

        Args:
            results: 所有智能体的执行结果
            intention_data: 原始意图识别结果

        Returns:
            聚合后的最终结果
        """
        aggregated = {
            "status": "completed",
            "intention": {
                "intents": intention_data.get("intents", []),
                "key_entities": intention_data.get("key_entities", {})
            },
            "agents_executed": len(results),
            "results": []
        }

        # 收集每个智能体的结果
        for result in results:
            aggregated["results"].append({
                "agent_name": result["agent_name"],
                "priority": result["priority"],
                "on_failure": result.get("on_failure", "abort"),
                "status": result["result"].get("status", "unknown"),
                "duration_sec": result["result"].get("duration_sec"),
                "attempts": result["result"].get("attempts", 1),
                "data": result["result"].get("data", {}),
                "error_code": result["result"].get("error_code"),
                "error_message": result["result"].get("error_message"),
                "retryable": bool(result["result"].get("retryable", False)),
            })

        # 检查是否有错误
        errors = [r for r in results if (r.get("result") or {}).get("status") == "error"]
        required_errors = [r for r in errors if r.get("on_failure", "abort") == "abort"]
        skipped = [r for r in results if (r.get("result") or {}).get("status") == "skipped"]
        if required_errors:
            aggregated["status"] = "failed"
        elif errors:
            aggregated["status"] = "partial_failure"
        aggregated["summary"] = {
            "success": len(results) - len(errors) - len(skipped),
            "error": len(errors),
            "skipped": len(skipped),
        }

        return aggregated

    def _message_for_non_skill_intent(self, intent: str) -> str:
        if intent == "unsupported":
            return (
                "这个问题不属于公司差旅规划或报销范围，我暂时无法处理。"
                "我可以帮你查询差旅政策、规划出差路线，或准备报销材料。"
            )
        return "我还不太确定这是否与公司差旅有关。请补充出差目的地、日期，或说明要查询的政策和报销问题。"

    def _update_memory(self, intention_data: Dict[str, Any], results: List[Dict]):
        """
        更新记忆系统

        Args:
            intention_data: 意图识别结果
            results: 智能体执行结果
        """
        if not self.memory_manager:
            return

        # 提取并保存信息到长期记忆
        for result in results:
            agent_name = result["agent_name"]
            data = result["result"].get("data", {})

            if agent_name == "event_collection" and isinstance(data, dict):
                event_data = data.get("data") if isinstance(data.get("data"), dict) else data
                event_data = filter_safe_memory_mapping(event_data)
                if any(event_data.get(key) for key in ("origin", "destination", "start_date", "end_date", "work_location")):
                    self.memory_manager.update_active_trip(event_data)

            # 如果是偏好智能体，保存偏好信息到长期记忆
            if agent_name == "preference" and isinstance(data, dict):
                preferences_data = data.get("preferences", {})

                # 新格式：preferences 是列表，包含 {type, value, action}
                if isinstance(preferences_data, list):
                    for pref_item in preferences_data:
                        if not isinstance(pref_item, dict):
                            continue

                        pref_type = pref_item.get("type")
                        pref_value = pref_item.get("value")
                        pref_action = pref_item.get("action", "replace")  # 默认覆盖

                        if not pref_type or not pref_value:
                            continue
                        if not is_safe_preference_value(pref_value):
                            logger.warning("Skipped sensitive preference value for %s", pref_type)
                            continue

                        # 根据 action 决定操作
                        if pref_action == "append":
                            # 追加模式：获取现有值并追加
                            current_prefs = self.memory_manager.long_term.get_preference()
                            existing_value = current_prefs.get(pref_type)

                            # 如果现有值是列表，追加
                            if isinstance(existing_value, list):
                                if pref_value not in existing_value:
                                    existing_value.append(pref_value)
                                self.memory_manager.long_term.save_preference(pref_type, existing_value)
                                logger.info(f"Appended to {pref_type}: {pref_value}, total: {existing_value}")
                            else:
                                # 如果现有值不是列表，创建新列表
                                new_list = [existing_value, pref_value] if existing_value else [pref_value]
                                self.memory_manager.long_term.save_preference(pref_type, new_list)
                                logger.info(f"Created list for {pref_type}: {new_list}")
                        else:
                            # 覆盖模式：直接保存新值
                            self.memory_manager.long_term.save_preference(pref_type, pref_value)
                            logger.info(f"Replaced {pref_type}: {pref_value}")

                # 旧格式兼容：preferences 是字典
                elif isinstance(preferences_data, dict):
                    for pref_type, value in preferences_data.items():
                        if value and pref_type != "has_preferences" and pref_type != "error":
                            if not is_safe_preference_value(value):
                                logger.warning("Skipped sensitive preference value for %s", pref_type)
                                continue
                            self.memory_manager.long_term.save_preference(pref_type, value)
                            logger.info(f"Updated {pref_type}: {value} (legacy format)")

            # 如果是行程规划智能体，保存行程到长期记忆
            if agent_name == "itinerary_planning" and isinstance(data, dict):
                itinerary = data.get("itinerary", {})
                planning_complete = data.get("planning_complete")

                # 只要有行程信息就保存（不管是否完全规划好）
                if itinerary and planning_complete is not False and not data.get("error"):
                    # 提取事项收集的信息（出发地、目的地等）
                    event_data = {}
                    for r in results:
                        if r["agent_name"] == "event_collection":
                            event_data = r["result"].get("data", {})
                            if isinstance(event_data.get("data"), dict):
                                event_data = event_data["data"]
                            event_data = filter_safe_memory_mapping(event_data)
                            break

                    # 从 event_data 获取行程信息
                    origin = event_data.get("origin")
                    destination = event_data.get("destination")
                    start_date = event_data.get("start_date")
                    end_date = event_data.get("end_date")
                    purpose = event_data.get("trip_purpose", "公司出差")

                    # 保存到长期记忆（只要有目的地就保存）
                    if destination:
                        self.memory_manager.long_term.save_trip_history({
                            "origin": origin,
                            "destination": destination,
                            "start_date": start_date,
                            "end_date": end_date,
                            "purpose": purpose,
                            "request_id": getattr(self.memory_manager, "current_request_id", None),
                        })
                        self.memory_manager.complete_active_trip(reason="planning_completed")
                        logger.info(f"Saved trip to long-term memory: {origin} -> {destination}")

        logger.info("Memory updated after orchestration")
