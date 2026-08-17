"""
Hommey 商旅助手 - Web 界面管理器
管理多用户 Hommey 实例的生命周期
"""
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Callable, Optional, TypeVar

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agents.intention_agent import IntentionAgent
from agents.orchestration_agent import OrchestrationAgent
from settings import (
    CONCURRENCY_CONFIG,
    MEMORY_CONFIG,
    RESILIENCE_CONFIG,
)
from context.memory_manager import MemoryManager
from context.async_memory import AsyncMemoryFacade
from runtime import create_agent_runtime, create_circuit_breaker
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.llm_resilience import retry_with_backoff
from utils.redis_coordination import (
    create_distributed_lock,
    create_redis_semaphore,
)
from redis.exceptions import RedisError
from utils.logging_safety import sanitize_for_log
from utils.io_executor import IoExecutorSaturated, run_blocking
from utils.memory_safety import redact_sensitive_text, wrap_untrusted_memory
from utils.observability import COMPONENT_LLM, ERROR_CIRCUIT_OPEN, record_upstream_error
from webui_new.core.errors import AppError, BusinessError, InternalError, UpstreamError
from core.onboarding import InitialPreferenceOnboarding
from core.intent_guard import is_pure_chitchat
from core.intent_router import FastIntentRouter, message_for_non_skill_intent
from core.intent_catalog import INTENT_DISPLAY_NAMES, updates_preferences_for_agent
from core.orchestration.memory_hooks import MemoryHookExecutor
from core.orchestration.pipeline import MultiIntentPipeline
from core.orchestration.state_store import OrchestrationStateStore, StateConflictError
from core.orchestration.turn_resolver import TurnResolver
from core.orchestration.validator import supports_task_pipeline
from core.execution_budget import (
    ExecutionBudget,
    ExecutionLimitExceeded,
    consume_agent_call,
    execution_budget_scope,
)
from core.presentation import RetrievalPresentation
from multimodal.schemas import NormalizedInput
from context.memory_repository import AttachmentBindingError
from evaluation.collector import (
    TurnEvaluationCollector,
    current_collector,
    reset_current_collector,
    set_current_collector,
)
from evaluation.sink import evaluation_sink

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 智能体显示名称（统一来源：core.intent_catalog）
AGENT_DISPLAY_NAMES = INTENT_DISPLAY_NAMES


class HommeyWebInstance:
    """单个用户的 Hommey 实例"""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.session_id = str(uuid.uuid4())[:8]
        self.memory_manager: Optional[MemoryManager] = None
        self.async_memory: Optional[AsyncMemoryFacade] = None
        self.orchestrator: Optional[OrchestrationAgent] = None
        self.intention_agent: Optional[IntentionAgent] = None
        self.attachment_service = None  # 多模态附件服务（runtime 注入；详见方案 §4.5）
        self.model = None
        self.multi_intent_pipeline: Optional[MultiIntentPipeline] = None
        self.state_store: Optional[OrchestrationStateStore] = None
        self._agent_cache = {}
        self.circuit_breaker: Optional[CircuitBreaker] = None
        self.onboarding = InitialPreferenceOnboarding()
        self.initialized = False
        self.init_error: Optional[str] = None

        self._total_messages: int = 0              # 本会话消息计数
        self._last_activity_monotonic: Optional[float] = None

    async def initialize(self):
        """Initialize the shared Hommey runtime for this web user."""
        try:
            runtime = create_agent_runtime(
                user_id=self.user_id,
                session_id=self.session_id,
                agent_cache=self._agent_cache,
            )

            self.model = runtime.model
            self.memory_manager = runtime.memory_manager
            self.async_memory = AsyncMemoryFacade(self.memory_manager)
            self.session_id = self.memory_manager.session_id
            self.intention_agent = runtime.intention_agent
            self.orchestrator = runtime.orchestrator
            self.state_store = OrchestrationStateStore(user_id=self.user_id)
            self.multi_intent_pipeline = MultiIntentPipeline(
                model=self.model,
                composer_model=runtime.composer_model,
                agent_runner=self.orchestrator.execute_task,
                memory_hooks=MemoryHookExecutor(self.memory_manager),
                state_store=self.state_store,
            )
            self.attachment_service = getattr(runtime, "attachment_service", None)
            self._agent_cache = runtime.agent_cache
            self.circuit_breaker = create_circuit_breaker()

            self.initialized = True
        except Exception as e:
            self.init_error = "初始化失败，请稍后刷新页面重试"
            logger.error("Init failed for user %s: %s", self.user_id, sanitize_for_log(e))
            raise

    def _ensure_async_memory(self) -> Optional[AsyncMemoryFacade]:
        """Lazily materialize the async facade once memory_manager is available.

        测试/轻量适配常直接赋值 memory_manager 而不走 initialize()，这里惰性包装，
        避免同步记忆 I/O 阻塞事件循环。
        """
        if self.async_memory is None and self.memory_manager is not None:
            self.async_memory = AsyncMemoryFacade(self.memory_manager)
        return self.async_memory

    async def get_preferences(self) -> dict:
        """获取用户偏好"""
        if not self.memory_manager:
            return {"preferences": [], "raw": {}}
        prefs = await self._ensure_async_memory().get_preference()
        if not prefs:
            return {"preferences": [], "raw": {}}
        # 转换为前端友好格式
        display_map = {
            "home_location": ("常驻地", "📍"),
            "transportation_preference": ("出行偏好", "🚄"),
            "hotel_brands": ("常住酒店", "🏨"),
            "airlines": ("常用航空", "✈️"),
            "seat_preference": ("座位偏好", "💺"),
            "meal_preference": ("餐食偏好", "🍜"),
            "budget_level": ("预算等级", "💰"),
        }
        result = []
        for key, value in prefs.items():
            if value:
                label, icon = display_map.get(key, (key, "📋"))
                display_value = value
                if isinstance(value, list):
                    display_value = " · ".join(str(v) for v in value)
                result.append({"icon": icon, "label": label, "value": display_value})
        return {"preferences": result, "raw": prefs}

    async def is_new_user(self) -> bool:
        """检查是否为新用户（没有任何偏好设置）"""
        if not self.memory_manager:
            return True
        return await run_blocking(self.onboarding.needs_onboarding, self.memory_manager)

    def list_chat_sessions(self) -> list[dict]:
        if not self.memory_manager:
            return []
        rows = self.memory_manager.long_term.get_chat_history(limit=None)
        titles = self.memory_manager.long_term.get_chat_session_titles()
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            session_id = row.get("session_id")
            if session_id:
                grouped.setdefault(str(session_id), []).append(row)

        sessions = []
        for session_id, messages in grouped.items():
            first_user = next(
                (item.get("content", "") for item in messages if item.get("role") == "user"),
                "",
            )
            last_message = messages[-1] if messages else {}
            generated_title = " ".join(str(first_user).split())[:30] or "未命名会话"
            sessions.append(
                {
                    "session_id": session_id,
                    "title": titles.get(session_id) or generated_title,
                    "preview": " ".join(str(last_message.get("content", "")).split())[:70],
                    "updated_at": last_message.get("timestamp", ""),
                    "message_count": len(messages),
                    "active": session_id == self.session_id,
                }
            )
        return sorted(
            sessions,
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )

    def get_chat_session(self, session_id: str) -> dict:
        if not self.memory_manager:
            return {"session_id": session_id, "messages": []}
        rows = self.memory_manager.long_term.get_chat_history(
            limit=None,
            session_id=session_id,
        )
        rows = self._recover_legacy_presentation_documents(rows)
        titles = self.memory_manager.long_term.get_chat_session_titles()
        return {
            "session_id": session_id,
            "title": titles.get(session_id),
            "messages": self._with_attachments(rows),
        }

    def _recover_legacy_presentation_documents(self, rows: list[dict]) -> list[dict]:
        """Repair typed intake cards that older canonical rows stored as text."""
        from core.presentation import recover_trip_intake_document

        repository = getattr(
            getattr(self.memory_manager, "memory_service", None), "repository", None,
        )
        recovered_rows = []
        for original in rows:
            row = dict(original)
            if (
                row.get("role") == "assistant"
                and not row.get("answer_document")
                and not row.get("presentation_document")
            ):
                document = recover_trip_intake_document(row.get("content") or "")
                if document is not None:
                    payload = document.model_dump(mode="json")
                    row["presentation_document"] = payload
                    if repository is not None and row.get("message_id"):
                        try:
                            repository.update_message_documents(
                                user_id=self.user_id,
                                message_id=row["message_id"],
                                presentation_document=payload,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Could not persist repaired intake card message_id=%s: %s",
                                row.get("message_id"), sanitize_for_log(exc),
                            )
            recovered_rows.append(row)
        return recovered_rows

    def _with_attachments(self, rows: list[dict]) -> list[dict]:
        """给历史消息附带其绑定的附件清单（供前端渲染附件卡片）。仅在附件服务可用时生效。"""
        if not self.attachment_service or not rows:
            return rows
        repository = getattr(self.attachment_service, "repository", None)
        if repository is None:
            return rows
        enriched = []
        for row in rows:
            message_id = row.get("message_id")
            if row.get("role") == "user" and message_id:
                try:
                    attachments = repository.attachments_for_message(message_id)
                    if attachments:
                        row["attachments"] = [
                            {"id": a.id, "filename": a.filename, "kind": a.kind}
                            for a in attachments
                        ]
                except Exception as e:
                    logger.warning("加载消息附件失败 message_id=%s: %s", message_id, sanitize_for_log(e))
            enriched.append(row)
        return enriched

    def start_new_chat_session(self) -> str:
        session_id = str(uuid.uuid4())
        if self.memory_manager:
            session_id = self.memory_manager.rotate_session(session_id)
        self.session_id = session_id
        self._last_activity_monotonic = None
        self._total_messages = 0
        return session_id

    def activate_chat_session(self, session_id: str) -> dict:
        payload = self.get_chat_session(session_id)
        if not payload["messages"]:
            raise ValueError("Chat session not found")
        if self.memory_manager:
            session_id = self.memory_manager.activate_session(session_id)
        self.session_id = session_id
        self._last_activity_monotonic = time.monotonic()
        self._total_messages = len(payload["messages"])
        return payload

    def rename_chat_session(self, session_id: str, title: str) -> None:
        if not self.memory_manager:
            raise ValueError("Memory is not initialized")
        if not self.get_chat_session(session_id)["messages"]:
            raise ValueError("Chat session not found")
        self.memory_manager.long_term.rename_chat_session(session_id, title)

    def delete_chat_session(self, session_id: str) -> str:
        if not self.memory_manager:
            raise ValueError("Memory is not initialized")
        self.memory_manager.long_term.delete_chat_session(session_id)
        if session_id == self.session_id:
            return self.start_new_chat_session()
        return self.session_id

    def clear_chat_history(self) -> str:
        if not self.memory_manager:
            raise ValueError("Memory is not initialized")
        self.memory_manager.long_term.clear_chat_history()
        return self.start_new_chat_session()

    async def get_onboarding_state(self) -> dict:
        """Return first-run preference setup progress."""
        if not self.memory_manager:
            return {"is_new": True, "completed": False, "missing_keys": []}
        return await run_blocking(self.onboarding.get_state, self.memory_manager)

    async def save_onboarding_preference(self, key: str, value: str) -> dict:
        """Save one first-run preference without using the chat pipeline."""
        if not self.memory_manager:
            raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")
        return await run_blocking(self.onboarding.save_answer, self.memory_manager, key, value)

    async def get_user_summary(self) -> dict:
        """获取用户摘要信息（用于右侧面板）"""
        prefs = await self.get_preferences()
        name_display = self.user_id
        if prefs["raw"].get("name"):
            name_display = prefs["raw"]["name"]
        return {
            "user_id": self.user_id,
            "name_display": name_display,
            "preferences": prefs["preferences"],
            "member_level": "白银会员",
            "member_tag": "差旅常客",
        }

    async def get_active_trip(self) -> dict:
        if not self.memory_manager:
            return {"active_trip": None}
        return {"active_trip": await self._ensure_async_memory().get_active_trip()}

    async def interrupt_active_turn(
        self, request_id: str, session_id: str | None = None,
    ) -> dict:
        """Interrupt the current turn without acquiring the request's long-held lock."""
        if self.state_store is None:
            raise BusinessError("NO_ACTIVE_RUN", "当前没有正在执行的任务")
        state = await self.state_store.get_active(session_id or self.session_id)
        if state is None or state.status not in {"ACTIVE", "INTERRUPTING"}:
            raise BusinessError("NO_ACTIVE_RUN", "当前没有正在执行的任务")
        try:
            state = await self.state_store.request_interrupt(
                state.run_id, state.current_turn_id or "", request_id=request_id,
            )
        except StateConflictError as exc:
            raise BusinessError("STALE_INTERRUPT", "这次执行已经结束或被新的执行替代") from exc
        return {
            "run_id": state.run_id,
            "turn_id": state.current_turn_id,
            "status": state.status,
            "revision": state.revision,
            "resumable": True,
        }

    @staticmethod
    def _is_simple_chitchat(message: str) -> bool:
        """快速判断是否纯闲聊（不经过 LLM）

        复用 is_pure_chitchat 的残余文本判定：带业务前缀的问候/感谢
        （如"谢谢，餐补多少"）不再被拦截，进入完整意图识别。
        """
        msg = message.strip().lower()
        # 单字/简单表情（保留原有快速路径）
        if len(msg) <= 2 and msg in ("嗯", "哦", "啊", "好", "行", "ok"):
            return True
        return is_pure_chitchat(msg)

    def _normalize_input(
        self, message: str, attachment_ids: list[str] | None
    ) -> NormalizedInput:
        """Build the one typed input used by persistence and agent execution."""
        if not attachment_ids:
            text = (message or "").strip()
            return NormalizedInput(agent_query=text, display_message=text)
        if self.attachment_service is None:
            raise InternalError("ATTACHMENT_SERVICE_UNAVAILABLE", "附件服务暂时不可用，请稍后重试")
        try:
            return self.attachment_service.normalize(message, attachment_ids, self.user_id)
        except AppError:
            raise
        except Exception as exc:
            # Fail closed: an attached file must never be silently ignored.
            raise InternalError(
                "ATTACHMENT_NORMALIZATION_FAILED",
                "附件处理失败，请重试或移除附件",
            ) from exc

    async def _persist_user_message_async(self, content: str, metadata: dict) -> None:
        """Persist display text and attachment links through one safe boundary (async)."""
        facade = self._ensure_async_memory()
        if facade is None:
            raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")
        try:
            await facade.add_message("user", content, metadata)
        except AttachmentBindingError as exc:
            raise BusinessError(
                "ATTACHMENT_BINDING_FAILED",
                "附件已失效或已被其他消息使用，请重新上传",
            ) from exc

    def _ensure_active_session(self) -> bool:
        """Resume or rotate the durable session after the idle timeout."""
        ensure_session = getattr(self.memory_manager, "ensure_active_session", None)
        if callable(ensure_session):
            rotated = ensure_session()
            self.session_id = self.memory_manager.session_id
        else:
            # Compatibility for lightweight adapters and isolated test doubles.
            now = time.monotonic()
            timeout = int(MEMORY_CONFIG.get("short_term", {}).get("session_idle_timeout_sec", 600))
            rotated = bool(
                self._last_activity_monotonic is not None
                and now - self._last_activity_monotonic >= max(timeout, 1)
            )
            if rotated:
                self.session_id = str(uuid.uuid4())[:8]
                rotate_session = getattr(self.memory_manager, "rotate_session", None)
                if callable(rotate_session):
                    rotate_session(self.session_id)
            self._last_activity_monotonic = now
        if rotated:
            self._total_messages = 0
        return rotated

    async def _handle_task_lifecycle_command(self, message: str) -> Optional[str]:
        """Handle explicit, narrowly-scoped current-task completion/cancellation commands."""
        normalized = "".join(message.strip().lower().split())
        cancel_commands = {
            "取消当前行程", "取消这个行程", "这个行程取消", "这个行程不安排了", "不安排这个行程了",
        }
        complete_commands = {
            "完成当前行程", "结束当前行程", "当前行程完成了", "这个行程完成了", "行程规划完成",
        }
        if normalized in cancel_commands:
            cancelled = await self._ensure_async_memory().cancel_active_trip()
            if self.state_store is not None:
                active = await self.state_store.get_active(self.session_id)
                if active is not None:
                    await self.state_store.finish_run(active.run_id, "ABANDONED")
            return "已取消当前行程任务。" if cancelled else "当前没有进行中的行程任务。"
        if normalized in complete_commands:
            completed = await self._ensure_async_memory().complete_active_trip(reason="user_completed")
            if self.state_store is not None:
                active = await self.state_store.get_active(self.session_id)
                if active is not None:
                    await self.state_store.finish_run(active.run_id, "COMPLETED")
            return "已结束当前行程任务。" if completed else "当前没有进行中的行程任务。"
        return None

    async def process_message(
        self,
        message: str,
        request_id: str | None = None,
        attachment_ids: list[str] | None = None,
        retrieval_mode: str = "standard",
        progress_callback=None,
    ) -> dict:
        """Run one user request inside an isolated execution budget and deadline."""
        retrieval_mode = "enhanced" if retrieval_mode == "enhanced" else "standard"
        rc = RESILIENCE_CONFIG
        budget = ExecutionBudget(
            max_agent_calls=rc.get("max_agent_calls_per_request", 8),
            max_external_calls=rc.get("max_external_calls_per_request", 16),
            max_external_calls_per_type=rc.get("max_external_calls_per_type", 6),
        )
        collector = None
        collector_token = None
        if getattr(evaluation_sink, "enabled", False):
            collector = TurnEvaluationCollector(
                request_id=request_id,
                session_id=self.session_id,
                user_message=message,
            )
            collector_token = set_current_collector(collector)
        try:
            with execution_budget_scope(budget):
                implementation_kwargs = {
                    "request_id": request_id,
                    "attachment_ids": attachment_ids,
                    "retrieval_mode": retrieval_mode,
                }
                if progress_callback is not None:
                    implementation_kwargs["progress_callback"] = progress_callback
                result = await asyncio.wait_for(
                    self._process_message_impl(message, **implementation_kwargs),
                    timeout=rc.get("request_timeout_sec", 120.0),
                )
                if collector is not None and not result.get("idempotent_replay") and not result.get("interrupted"):
                    try:
                        collector.turn_id = str(
                            getattr(self.memory_manager, "_current_turn_id", "") or collector.turn_id
                        )
                        evaluation_sink.try_emit(collector.freeze(result, session_id=self.session_id))
                    except Exception as exc:
                        logger.warning(
                            "evaluation_capture_failed request_id=%s error=%s",
                            collector.request_id,
                            sanitize_for_log(exc),
                        )
                return result
        except ExecutionLimitExceeded as exc:
            raise UpstreamError(
                exc.code,
                exc.public_message,
                retryable=False,
                component=COMPONENT_LLM,
                debug_message=str(exc),
            ) from exc
        except IoExecutorSaturated as exc:
            raise UpstreamError(
                "IO_EXECUTOR_SATURATED",
                "服务暂时繁忙，请稍后再试。",
                retryable=True,
                component=COMPONENT_LLM,
                debug_message=str(exc),
            ) from exc
        except asyncio.TimeoutError as exc:
            logger.error(
                "Request execution timed out user_id=%s budget=%s",
                self.user_id,
                budget.snapshot(),
            )
            raise UpstreamError(
                "REQUEST_EXECUTION_TIMEOUT",
                "本次任务处理超时，请稍后重试。",
                retryable=True,
                component=COMPONENT_LLM,
                debug_message=str(exc),
            ) from exc
        finally:
            if collector_token is not None:
                reset_current_collector(collector_token)
            logger.info("Request execution budget user_id=%s budget=%s", self.user_id, budget.snapshot())

    async def _process_message_impl(
        self,
        message: str,
        request_id: str | None = None,
        attachment_ids: list[str] | None = None,
        retrieval_mode: str = "standard",
        progress_callback=None,
    ) -> dict:
        """处理用户消息，返回响应"""
        from agentscope.message import Msg

        start_time = time.perf_counter()
        timings = {}

        if not self.initialized:
            raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")

        await run_blocking(self._ensure_active_session)
        if request_id:
            get_recorded_response = getattr(self.memory_manager, "get_recorded_response", None)
            recorded = (
                await run_blocking(get_recorded_response, request_id)
                if get_recorded_response else None
            )
            if recorded:
                get_recorded_document = getattr(
                    self.memory_manager, "get_recorded_answer_document", None
                )
                get_recorded_presentation = getattr(
                    self.memory_manager, "get_recorded_presentation_document", None
                )
                recorded_document = (
                    await run_blocking(get_recorded_document, request_id)
                    if get_recorded_document else None
                )
                recorded_presentation = (
                    await run_blocking(get_recorded_presentation, request_id)
                    if get_recorded_presentation else None
                )
                return {
                    "response": recorded,
                    "answer_document": recorded_document,
                    "presentation_document": recorded_presentation,
                    "agents": [],
                    "preferences_updated": False,
                    "idempotent_replay": True,
                }
        metadata = {"request_id": request_id} if request_id else {}
        if self.memory_manager is not None:
            self.memory_manager.current_request_id = request_id
        self._ensure_async_memory()

        normalized = self._normalize_input(message, attachment_ids)
        agent_query = normalized.agent_query
        display_message = normalized.display_message
        user_metadata = {
            **metadata,
            "attachment_ids": normalized.attachment_ids,
            "content_type": "attachment" if normalized.attachment_ids else "text",
            "retrieval_mode": retrieval_mode,
        }
        input_result = {
            "sources": [source.model_dump() for source in normalized.sources],
            "warnings": list(normalized.warnings),
        }

        active_run = (
            await self.state_store.get_active(self.session_id)
            if self.state_store is not None else None
        )
        # process_message is already inside the per-user local + distributed
        # lock.  If a different request still sees ACTIVE here, the owning
        # process died after its latest durable boundary; make it resumable
        # before resolving this Turn.
        if (
            active_run is not None
            and active_run.status == "ACTIVE"
            and active_run.current_request_id != (request_id or "")
        ):
            active_run = await self.state_store.recover_orphaned_active_run(
                active_run.run_id, incoming_request_id=request_id or "",
            )
        relation = TurnResolver.resolve(message, active_run) if active_run is not None else None
        resume_state = active_run if relation and relation.kind == "resume" else None

        # “继续上次任务”在当前会话没有目标时，只列出其他会话候选，不跨会话猜测。
        if active_run is None and "".join(message.strip().lower().split()) in {
            "继续", "接着做", "继续执行", "继续上次任务", "恢复任务",
        } and self.state_store is not None:
            candidates = await self.state_store.list_resumable()
            if candidates:
                response = (
                    "当前对话没有暂停任务。其他对话中有可恢复任务，请先切换到对应对话再继续。"
                    if len(candidates) == 1 else
                    f"其他对话中有 {len(candidates)} 个可恢复任务，请先选择对应对话。"
                )
                await self._persist_user_message_async(display_message, user_metadata)
                await self.async_memory.add_message("assistant", response, metadata)
                return {"response": response, "agents": [], "preferences_updated": False, **input_result}

        lifecycle_response = await self._handle_task_lifecycle_command(message)
        if lifecycle_response:
            await self._persist_user_message_async(display_message, user_metadata)
            await self.async_memory.add_message("assistant", lifecycle_response, metadata)
            return {
                "response": lifecycle_response,
                "agents": [],
                "preferences_updated": False,
                **input_result,
            }

        # ═══ 优化 1: 简单闲聊直接处理，不经过 LLM ═══
        # 带附件时不走闲聊短路，避免附件问题被草率打发。
        if resume_state is None and self._is_simple_chitchat(message) and not attachment_ids:
            collector = current_collector()
            if collector is not None:
                collector.record_routing({
                    "routing": {"intent": "chitchat", "should_call_skill": True},
                    "intents": [{"type": "chitchat", "should_call_skill": True}],
                })
            await self._persist_user_message_async(display_message, user_metadata)
            response = await self._handle_chitchat(message)
            await self.async_memory.add_message("assistant", response, metadata)
            return {
                "response": response,
                "agents": [],
                "preferences_updated": False,
                **input_result,
            }

        rc = RESILIENCE_CONFIG
        agent_max_retries = rc.get("agent_max_retries", 1)
        # 带附件时强制走完整意图链路（_build_context 会把 agent_query 含附件上下文喂给 LLM），
        # 不走绕过上下文的 fast_route，避免附件文本无法到达模型。
        fast_route = None if attachment_ids or active_run is not None else self._route_without_context(message)

        if resume_state is not None:
            intention_data = resume_state.intention_data
            intention_result = Msg(
                name="StateCoordinator",
                content=json.dumps(intention_data, ensure_ascii=False),
                role="assistant",
            )
            timings["context"] = 0.0
            timings["intent"] = 0.0
        elif fast_route:
            intention_data = fast_route.to_intention_data(agent_query)
            intention_result = Msg(
                name="IntentionAgent",
                content=json.dumps(intention_data, ensure_ascii=False),
                role="assistant",
            )
            timings["context"] = 0.0
            timings["intent"] = 0.0
        else:
            # 意图层只读取当前任务和当前会话；历史与偏好由授权后的 skill 按需读取。
            context_future = asyncio.ensure_future(self._build_context(agent_query))

            # 2. Intent recognition
            try:
                if self.circuit_breaker:
                    await self.circuit_breaker.raise_if_open()

                context_start = time.perf_counter()
                context_messages = await context_future
                timings["context"] = time.perf_counter() - context_start

                intent_start = time.perf_counter()
                async def call_intention_agent():
                    consume_agent_call("IntentionAgent")
                    return await self.intention_agent.reply(context_messages)

                intention_result = await retry_with_backoff(
                    call_intention_agent,
                    max_retries=agent_max_retries,
                    base_delay_sec=rc.get("retry_base_delay_sec", 1.0),
                    max_delay_sec=rc.get("retry_max_delay_sec", 30.0),
                )
                timings["intent"] = time.perf_counter() - intent_start
                if self.circuit_breaker:
                    await self.circuit_breaker.record_success()
            except ExecutionLimitExceeded:
                raise
            except CircuitOpenError:
                record_upstream_error(COMPONENT_LLM, ERROR_CIRCUIT_OPEN, retryable=True)
                raise UpstreamError("CIRCUIT_OPEN", "服务暂时不可用，请稍后再试。", retryable=True, component=COMPONENT_LLM)
            except Exception as e:
                if self.circuit_breaker:
                    await self.circuit_breaker.record_failure()
                logger.error("Intention agent failed: %s", sanitize_for_log(e))
                record_upstream_error(COMPONENT_LLM, e, retryable=True)
                raise UpstreamError(
                    "INTENTION_FAILED",
                    "处理请求时出错，请稍后重试。",
                    retryable=True,
                    component=COMPONENT_LLM,
                    debug_message=str(e),
                )

        try:
            intention_data = json.loads(intention_result.content)
        except json.JSONDecodeError:
            raise UpstreamError(
                "INTENTION_PARSE_FAILED",
                "抱歉，我没能理解您的意思，请换一种说法试试？",
                retryable=False,
                component=COMPONENT_LLM,
            )

        collector = current_collector()
        if collector is not None:
            collector.record_routing(intention_data)

        self._total_messages += 1
        # Persistence boundary: display text and attachment links commit together.
        await self._persist_user_message_async(display_message, user_metadata)

        request_context = {
            "original_query": message,
            "agent_query": normalized.agent_query,
            "retrieval_mode": retrieval_mode,
            "request_id": request_id or "",
            "attachment_sources": input_result["sources"],
            "attachment_warnings": input_result["warnings"],
        }

        # 所有已授权的 skill 意图都走统一 scoped DAG 管线。
        use_task_pipeline = bool(
            self.multi_intent_pipeline is not None
            and supports_task_pipeline(intention_data)
        )
        if use_task_pipeline:
            try:
                orchestration_start = time.perf_counter()
                base_context = self.orchestrator.prepare_context(
                    intention_data,
                    request_context=request_context,
                )
                if resume_state is not None:
                    pipeline_output = await self.multi_intent_pipeline.resume_run(
                        state=resume_state,
                        original_query=display_message,
                        base_context=base_context,
                        progress=progress_callback,
                        request_id=request_id or "",
                    )
                else:
                    pipeline_output = await self.multi_intent_pipeline.run(
                        original_query=display_message,
                        intention_data=intention_data,
                        base_context=base_context,
                        progress=progress_callback,
                        task_query=normalized.agent_query,
                        # An independent question may be answered while another
                        # goal is waiting; append it to the same durable Run.
                        session_id=self.session_id,
                        request_id=request_id or "",
                        existing_state=active_run,
                    )
                timings["orchestration"] = time.perf_counter() - orchestration_start
                if collector is not None:
                    collector.record_pipeline(pipeline_output)
                self.orchestrator.record_task_results(intention_data, pipeline_output.results)
                if self.circuit_breaker:
                    await self.circuit_breaker.record_success()
            except ExecutionLimitExceeded:
                raise
            except CircuitOpenError:
                record_upstream_error(COMPONENT_LLM, ERROR_CIRCUIT_OPEN, retryable=True)
                raise UpstreamError(
                    "CIRCUIT_OPEN",
                    "服务暂时不可用，请稍后再试。",
                    retryable=True,
                    component=COMPONENT_LLM,
                )
            except Exception as e:
                if self.circuit_breaker:
                    await self.circuit_breaker.record_failure()
                logger.error("Task orchestration failed: %s", sanitize_for_log(e))
                record_upstream_error(COMPONENT_LLM, e, retryable=True)
                raise UpstreamError(
                    "ORCHESTRATION_FAILED",
                    "调度执行失败，请稍后重试。",
                    retryable=True,
                    component=COMPONENT_LLM,
                    debug_message=str(e),
                )
            if pipeline_output.paused:
                return await self._task_pipeline_paused(
                    pipeline_output, metadata, start_time, timings
                )
            if pipeline_output.interrupted:
                timings["total"] = time.perf_counter() - start_time
                return {
                    "response": "",
                    "answer_document": None,
                    "presentation_document": None,
                    "agents": [],
                    "interrupted": True,
                    "preferences_updated": False,
                    "timings": {key: round(value, 3) for key, value in timings.items()},
                }
            # abort 语义的硬失败转入公共错误流；continue 降级（如天气不可用）走卡片。
            self._raise_on_pipeline_errors(pipeline_output)
            retrieval_presentation = self._retrieval_presentation(pipeline_output)
            if retrieval_presentation is not None:
                pipeline_output.answer_document.retrieval = retrieval_presentation
            answer_document = pipeline_output.answer_document.model_dump(mode="json")
            response = pipeline_output.answer_document.plain_text
            assistant_metadata = dict(metadata)
            assistant_metadata["answer_document"] = answer_document
            await self.async_memory.add_message("assistant", response, assistant_metadata)
            agents = [
                {
                    "name": result.agent_name,
                    "display": AGENT_DISPLAY_NAMES.get(result.agent_name, result.agent_name),
                    "status": result.status,
                    "duration_sec": result.duration_sec,
                }
                for result in pipeline_output.results
            ]
            timings["total"] = time.perf_counter() - start_time
            return {
                "response": response,
                "answer_document": answer_document,
                "agents": agents,
                "preferences_updated": any(
                    updates_preferences_for_agent(r.agent_name) and r.status == "success"
                    for r in pipeline_output.results
                ),
                "timings": {key: round(value, 3) for key, value in timings.items()},
            }

        # 3. 非 skill 意图（unsupported/unclear/fallback）不进入任务管线：
        #    should_call_skill 为 False 时直接返回澄清；其余兜底闲聊。
        routing = intention_data.get("routing") or {}
        if routing.get("should_call_skill") is False:
            response = (
                intention_data.get("clarification")
                or message_for_non_skill_intent(routing.get("intent"))
            )
        else:
            response = await self._handle_chitchat(agent_query)
        await self.async_memory.add_message("assistant", response, metadata)
        timings["total"] = time.perf_counter() - start_time
        return {
            "response": response,
            "answer_document": None,
            "presentation_document": None,
            "agents": [],
            "preferences_updated": False,
            "timings": {key: round(value, 3) for key, value in timings.items()},
            **input_result,
        }

    async def _task_pipeline_paused(self, pipeline_output, metadata, start_time, timings) -> dict:
        """跨轮暂停：返回 trip_intake presentation 契约，保持与 legacy 分支一致。"""
        presentation_document = pipeline_output.presentation_document.model_dump(mode="json")
        response = presentation_document["plain_text"]
        assistant_metadata = dict(metadata)
        assistant_metadata["presentation_document"] = presentation_document
        await self.async_memory.add_message("assistant", response, assistant_metadata)
        agents = [
            {
                "name": result.agent_name,
                "display": AGENT_DISPLAY_NAMES.get(result.agent_name, result.agent_name),
                "status": result.status,
                "duration_sec": result.duration_sec,
            }
            for result in pipeline_output.results
        ]
        timings["total"] = time.perf_counter() - start_time
        return {
            "response": response,
            "answer_document": None,
            "presentation_document": presentation_document,
            "agents": agents,
            "preferences_updated": False,
            "timings": {key: round(value, 3) for key, value in timings.items()},
        }

    @staticmethod
    def _retrieval_presentation(pipeline_output) -> RetrievalPresentation | None:
        for result in pipeline_output.results:
            if result.agent_name != "rag_knowledge":
                continue
            raw = result.data.get("retrieval") if isinstance(result.data, dict) else None
            if not isinstance(raw, dict) or raw.get("requested_mode") != "enhanced":
                continue
            try:
                return RetrievalPresentation.model_validate(raw)
            except Exception as exc:
                logger.warning(
                    "retrieval_presentation_invalid task_id=%s error=%s",
                    result.task_id,
                    sanitize_for_log(exc),
                )
        return None

    def _raise_on_pipeline_errors(self, pipeline_output) -> None:
        """把 pipeline 的 abort 语义硬失败转入公共错误流；continue 降级继续走卡片。

        TaskResult 的 error 不都等于整体失败：步骤声明 ``on_failure=continue``
        时（如天气/公开信息不可用）executor 会降级，不应打断响应；只有
        ``on_failure=abort`` 的错误步骤才作为整体失败上抛。
        """
        task_by_id = {task.task_id: task for task in pipeline_output.execution_tasks}
        errors = []
        for result in pipeline_output.results:
            if result.status != "error":
                continue
            task = task_by_id.get(result.task_id)
            if task is not None and task.failure_policy == "continue":
                continue
            errors.append({
                "agent_name": result.agent_name,
                "status": "error",
                "message": result.error_message or "",
                "error_code": result.error_code or "AGENT_EXECUTION_FAILED",
            })
        if errors:
            self._raise_on_agent_errors({"status": "error", "results": errors})

    def _raise_on_agent_errors(self, result_data: dict) -> None:
        """Convert internal agent error payloads into the public AppError flow."""
        errors = []
        for result in result_data.get("results", []):
            if result.get("status") != "error":
                continue
            errors.append({
                "agent_name": result.get("agent_name", "unknown"),
                "message": (
                    result.get("error_message")
                    or result.get("message")
                    or "agent returned error status"
                ),
                "error_code": result.get("error_code") or "AGENT_EXECUTION_FAILED",
            })

        if not errors:
            return

        first_error = errors[0]
        agent_name = first_error["agent_name"]
        debug_message = first_error["message"]
        error_code = first_error["error_code"]
        logger.error(
            "Agent result failed user_id=%s agent=%s error=%s",
            self.user_id,
            agent_name,
            sanitize_for_log(debug_message),
        )
        record_upstream_error(COMPONENT_LLM, str(debug_message), retryable=True)
        limit_codes = {
            "AGENT_CALL_LIMIT_EXCEEDED",
            "EXTERNAL_CALL_LIMIT_EXCEEDED",
            "EXTERNAL_CALL_TYPE_LIMIT_EXCEEDED",
        }
        is_limit_error = error_code in limit_codes
        raise UpstreamError(
            error_code if is_limit_error else "AGENT_EXECUTION_FAILED",
            str(debug_message) if is_limit_error else "处理失败，请稍后重试。",
            retryable=not is_limit_error,
            component=COMPONENT_LLM,
            debug_message=f"{agent_name}: {debug_message}",
        )

    async def stream_message(
        self,
        message: str,
        request_id: str | None = None,
        attachment_ids: list[str] | None = None,
        retrieval_mode: str = "standard",
    ):
        """Yield live orchestration progress and response events as NDJSON payloads."""
        queue: asyncio.Queue = asyncio.Queue()
        result_holder = {}

        async def progress_callback(event):
            payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else dict(event)
            await queue.put(payload)

        async def run_request():
            try:
                request_kwargs = {
                    "request_id": request_id,
                    "attachment_ids": attachment_ids,
                    "progress_callback": progress_callback,
                }
                if retrieval_mode == "enhanced":
                    request_kwargs["retrieval_mode"] = "enhanced"
                result_holder["result"] = await self.process_message(message, **request_kwargs)
            finally:
                await queue.put(None)

        yield {"type": "status", "phase": "analyzing", "message_key": "request_analyzing"}
        request_task = asyncio.create_task(run_request())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            # 生成器被关闭/取消（前端断连、外层取消）时，取消内层 request_task，
            # 避免孤儿任务在无锁状态下继续运行、写记忆并占用全局信号量计数。
            # 正常完成路径下（run_request 已自然结束）cancel 对已完成任务无效，
            # gather(return_exceptions=True) 吞掉 CancelledError/异常，二者均无害。
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        await request_task
        result = result_holder["result"]

        if result.get("interrupted"):
            yield {"type": "interrupted", "resumable": True}
            yield {
                "type": "done", "interrupted": True,
                "preferences_updated": False,
                "timings": result.get("timings", {}),
            }
            return

        if result.get("sources") or result.get("warnings"):
            yield {
                "type": "attachment_context",
                "sources": result.get("sources", []),
                "warnings": result.get("warnings", []),
            }

        agents = result.get("agents", [])
        if agents:
            yield {"type": "agents", "agents": agents}

        answer_document = result.get("answer_document")
        if answer_document:
            yield {"type": "answer_document", "document": answer_document}
            yield {
                "type": "done",
                "preferences_updated": result.get("preferences_updated", False),
                "timings": result.get("timings", {}),
            }
            return

        presentation_document = result.get("presentation_document")
        if presentation_document:
            yield {"type": "presentation_document", "document": presentation_document}
            yield {
                "type": "done",
                "preferences_updated": result.get("preferences_updated", False),
                "timings": result.get("timings", {}),
            }
            return

        response = result.get("response") or ""
        for chunk in self._chunk_text(response):
            yield {"type": "chunk", "text": chunk}
            await asyncio.sleep(0.01)

        yield {
            "type": "done",
            "preferences_updated": result.get("preferences_updated", False),
            "timings": result.get("timings", {}),
            "sources": result.get("sources", []),
            "warnings": result.get("warnings", []),
        }

    @staticmethod
    def _chunk_text(text: str, size: int = 18):
        for idx in range(0, len(text), size):
            yield text[idx:idx + size]

    def _route_without_context(self, message: str):
        """Run cheap routing before building memory context for context-free intents."""
        short_term = getattr(self.memory_manager, "short_term", None)
        if short_term is not None:
            try:
                if short_term.get_recent_context(n_turns=1):
                    return None
            except (AttributeError, TypeError):
                pass
        route = FastIntentRouter.route(message)
        if (
            not route or not route.should_call_skill
            or len(route.intent_types) != 1
            or not route.safe_to_short_circuit
        ):
            return None
        if route.intent_type in {"rag_knowledge", "information_query", "train_query", "chitchat"}:
            return route
        return None

    async def _build_context(self, message: str) -> list:
        """构建上下文消息（可与其他异步任务并行）"""
        from agentscope.message import Msg

        recent_context = await self.async_memory.get_recent_context(n_turns=5)

        collector = current_collector()
        if collector is not None:
            collector.record_context(recent_context)

        context_messages = []
        active_trip = await self.async_memory.get_active_trip()
        memory_parts = []
        if active_trip:
            memory_parts.extend([
                "【当前出差任务｜可用于补全当前问题】",
                json.dumps(active_trip, ensure_ascii=False),
            ])
        if memory_parts:
            context_messages.append(Msg(
                name="system",
                content=wrap_untrusted_memory("\n".join(memory_parts)),
                role="system",
            ))
        for msg in recent_context:
            context_messages.append(Msg(name=msg["role"], content=msg["content"], role=msg["role"]))
        context_messages.append(Msg(name="user", content=message, role="user"))

        return context_messages

    async def _handle_chitchat(self, user_input: str) -> str:
        """闲聊兜底（只用 skill 注册表，删除了硬编码脚本路径回退）。"""
        from agentscope.message import Msg

        try:
            agent = self.orchestrator.agent_registry["chitchat"]
        except (KeyError, Exception):
            agent = None

        if agent is None:
            return "嗯嗯，我听着呢～有什么出行相关的问题需要帮忙吗？😊"

        try:
            consume_agent_call(getattr(agent, "name", "chitchat"))
            input_msg = Msg(
                name="user",
                content=json.dumps({"query": user_input}, ensure_ascii=False),
                role="user",
            )
            response = await agent.reply(input_msg)
            data = json.loads(response.content) if isinstance(response.content, str) else response.content
            reply = data.get("response", "") if isinstance(data, dict) else str(data)
            return reply
        except ExecutionLimitExceeded:
            raise
        except Exception as e:
            logger.warning(f"Chitchat failed: {e}")
            return "嗯嗯，我听着呢～有什么出行相关的问题需要帮忙吗？😊"



class WebHommeyManager:
    """管理所有用户的 Hommey 实例"""

    def __init__(self):
        self._instances: dict[str, HommeyWebInstance] = {}
        self._user_locks: dict[str, asyncio.Lock] = {}

    def _per_user_lock(self, user_id: str) -> asyncio.Lock:
        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()
        return self._user_locks[user_id]

    def get_or_create(self, user_id: str) -> HommeyWebInstance:
        if user_id not in self._instances:
            self._instances[user_id] = HommeyWebInstance(user_id)
        return self._instances[user_id]

    def get(self, user_id: str) -> Optional[HommeyWebInstance]:
        return self._instances.get(user_id)

    async def initialize_user(self, user_id: str) -> HommeyWebInstance:
        """获取/创建用户实例并初始化，进程内 per-user 锁防止重复初始化。"""
        lock = self._per_user_lock(user_id)
        async with lock:
            instance = self.get_or_create(user_id)
            if not instance.initialized:
                await instance.initialize()
            return instance

    async def get_initialized_user(self, user_id: str) -> HommeyWebInstance:
        """Return a usable local instance, rebuilding it on a cold worker."""
        instance = self.get(user_id)
        if not instance or not instance.initialized:
            instance = await self.initialize_user(user_id)
        return instance

    @asynccontextmanager
    async def user_state_scope(self, user_id: str):
        """Serialize user/session state access across workers without an LLM slot."""
        await self.get_initialized_user(user_id)
        async with self._user_lock_scope(user_id, acquire_global_slot=False) as lock_lost:
            if lock_lost.is_set():
                raise self._lock_lost_error()
            instance = self.get(user_id)
            if not instance or not instance.initialized:
                raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")
            try:
                yield instance
            except IoExecutorSaturated as exc:
                raise UpstreamError(
                    "IO_EXECUTOR_SATURATED",
                    "服务暂时繁忙，请稍后再试。",
                    retryable=True,
                    component=COMPONENT_LLM,
                    debug_message=str(exc),
                ) from exc
            if lock_lost.is_set():
                raise self._lock_lost_error()

    async def run_user_state_operation(
        self,
        user_id: str,
        operation: Callable[[HommeyWebInstance], T],
    ) -> T:
        """Run one synchronous state operation off-loop under the user lock."""
        async with self.user_state_scope(user_id) as instance:
            return await run_blocking(operation, instance)

    async def interrupt_active_turn(
        self, user_id: str, request_id: str, session_id: str | None = None,
    ) -> dict:
        # Deliberately bypass _user_lock_scope: the active stream owns that lock.
        instance = self.get_or_create(user_id)
        if not instance.initialized:
            await instance.initialize()
        return await instance.interrupt_active_turn(request_id, session_id=session_id)

    @asynccontextmanager
    async def _user_lock_scope(self, user_id: str, *, acquire_global_slot: bool = True):
        """锁编排作用域：进程内锁 → 分布式锁 → 全局信号量，持锁心跳续约。

        本地锁与分布式锁共用一个 deadline（per_user_lock_timeout_sec），本地锁获取也
        纳入超时（asyncio.wait_for），避免本地锁无界等待；心跳续约失败（锁易主）时置
        lock_lost 事件，调用方在关键点检查并中止处理；退出时逆序释放，任何一步释放
        失败都不中断后续释放，本地锁必释放。

        Yield: asyncio.Event —— 锁易主时被 set，调用方应在关键点检查并中止处理。
        """
        rc = CONCURRENCY_CONFIG
        deadline = time.monotonic() + float(rc.get("per_user_lock_timeout_sec", 60.0))
        lock_lost = asyncio.Event()
        heartbeat = None

        local_lock = self._per_user_lock(user_id)
        distributed_lock = create_distributed_lock(f"hommey:lock:user:{user_id}")
        semaphore = create_redis_semaphore() if acquire_global_slot else None

        acquired_distributed = False
        acquired_semaphore = False

        # 1) 进程内 per-user 锁：统一 deadline 内获取，超时报 USER_QUEUE_TIMEOUT。
        #    本地等待不设额外超时（同一 worker 内先到先得），由 deadline 兜底。
        remaining = deadline - time.monotonic()
        try:
            await asyncio.wait_for(local_lock.acquire(), remaining)
        except asyncio.TimeoutError:
            raise UpstreamError(
                "USER_QUEUE_TIMEOUT",
                "您有请求正在处理，请稍候再试。",
                retryable=True,
                component=COMPONENT_LLM,
            ) from None

        try:
            # 2) 分布式锁：跨 worker 串行，同一 deadline。Redis 不可用时 fail closed
            #    （§3.3）：不允许绕过协调继续处理昂贵请求或用户状态写入。
            while not await self._acquire_or_fail(distributed_lock):
                if time.monotonic() >= deadline:
                    raise UpstreamError(
                        "USER_QUEUE_TIMEOUT",
                        "您有请求正在处理，请稍候再试。",
                        retryable=True,
                        component=COMPONENT_LLM,
                    )
                await asyncio.sleep(float(rc.get("lock_retry_interval_sec", 0.2)))
            acquired_distributed = True

            # 心跳续约：任何失败（renew 返回 False 或抛异常，如瞬时 Redis 连接错误）
            # 都置 lock_lost，主协程据此中止（Important 3）。否则任务静默死亡，
            # 锁/信号量租约过期后另一 worker 可取得，跨 worker 并发处理。锁与
            # 信号量租约都随心跳续约（§5.2/§5.3）。
            async def _heartbeat():
                while True:
                    await asyncio.sleep(float(rc.get("lock_heartbeat_interval_sec", 15.0)))
                    lock_ok = await self._renew_or_false(distributed_lock, user_id, "lock")
                    sem_ok = (
                        await self._renew_or_false(semaphore, user_id, "semaphore")
                        if acquired_semaphore and semaphore is not None
                        else True
                    )
                    if not (lock_ok and sem_ok):
                        lock_lost.set()
                        return

            heartbeat = asyncio.create_task(_heartbeat())

            # 3) 全局信号量：并发上限（token 化租约，Redis 不可用同样 fail closed）。
            if semaphore is not None:
                sem_deadline = time.monotonic() + float(rc.get("semaphore_acquire_timeout_sec", 120.0))
                while not await self._acquire_or_fail(semaphore):
                    if time.monotonic() >= sem_deadline:
                        raise UpstreamError(
                            "GLOBAL_CONCURRENCY_LIMIT",
                            "系统繁忙，请稍后再试。",
                            retryable=True,
                            component=COMPONENT_LLM,
                        )
                    await asyncio.sleep(float(rc.get("lock_retry_interval_sec", 0.2)))
                acquired_semaphore = True

            yield lock_lost
        finally:
            # 释放链：任何一步失败都不中断后续释放，本地锁必释放（Important 2）
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                if acquired_semaphore:
                    await semaphore.release()
            except Exception as e:
                logger.warning("semaphore release failed user_id=%s: %s", user_id, sanitize_for_log(e))
            try:
                if acquired_distributed:
                    await distributed_lock.release()
            except Exception as e:
                logger.warning("distributed lock release failed user_id=%s: %s", user_id, sanitize_for_log(e))
            local_lock.release()

    @staticmethod
    def _lock_lost_error() -> UpstreamError:
        return UpstreamError(
            "LOCK_LOST",
            "会话已超时，请重新发送消息。",
            retryable=True,
            component=COMPONENT_LLM,
        )

    @staticmethod
    async def _acquire_or_fail(primitive) -> bool:
        """调用协调原语 ``acquire()``，把 Redis 故障翻译为可重试错误（fail closed）。

        Redis 无法获取锁或租约时，不允许绕过协调继续处理昂贵请求或用户状态写入
        （§3.3）。连接失败/超时等 ``RedisError`` 统一映射为 ``REDIS_UNAVAILABLE``。
        """
        try:
            return bool(await primitive.acquire())
        except RedisError as exc:
            logger.error("coordination acquire failed: %s", sanitize_for_log(exc))
            raise UpstreamError(
                "REDIS_UNAVAILABLE",
                "服务暂时繁忙，请稍后再试。",
                retryable=True,
                component=COMPONENT_LLM,
            ) from exc

    @staticmethod
    async def _renew_or_false(primitive, user_id: str, label: str) -> bool:
        """续约协调原语；任何异常/失败都返回 False，由心跳据此置 lock_lost。"""
        try:
            return bool(await primitive.renew())
        except Exception as exc:  # noqa: BLE001 - renew 失败一律视为锁易主/不可用
            logger.warning(
                "coordination %s renew failed user_id=%s: %s",
                label,
                user_id,
                sanitize_for_log(exc),
            )
            return False

    async def process_message(
        self,
        user_id: str,
        message: str,
        *,
        request_id: str | None = None,
        attachment_ids: list[str] | None = None,
        retrieval_mode: str = "standard",
        progress_callback=None,
    ) -> dict:
        """统一消息入口：进程内锁 → 分布式锁 → 全局信号量，持锁心跳续约。

        取锁前懒初始化：跨 worker 场景下当前 worker 可能没有该用户实例（onboarding
        落在另一 worker），此时在取锁前复用 _per_user_lock 调用 initialize_user，
        与后续取锁分属不同锁段，避免 asyncio.Lock 不可重入造成的死锁。
        """
        instance = self.get(user_id)
        if not instance or not instance.initialized:
            await self.initialize_user(user_id)

        async with self._user_lock_scope(user_id) as lock_lost:
            # 锁已易主：不启动新的处理（Important 3）
            if lock_lost.is_set():
                raise self._lock_lost_error()

            # 锁内重新获取实例；初始化后仍无实例则兜底报 NOT_INITIALIZED。
            instance = self.get(user_id)
            if not instance or not instance.initialized:
                from webui_new.core.errors import BusinessError
                raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")

            # 与锁丢失事件竞争：锁易主即中止在途处理。
            # instance.process_message 内部已有 request_timeout_sec 的 wait_for，取消是既有可接受语义。
            instance_kwargs = {
                "request_id": request_id,
                "attachment_ids": attachment_ids,
                "progress_callback": progress_callback,
            }
            if retrieval_mode == "enhanced":
                instance_kwargs["retrieval_mode"] = "enhanced"
            process_task = asyncio.create_task(instance.process_message(message, **instance_kwargs))
            lost_task = asyncio.create_task(lock_lost.wait())
            try:
                _, pending = await asyncio.wait(
                    {process_task, lost_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except BaseException:
                # 外层取消（请求超时/关闭）：清理两个竞争任务，避免孤儿任务
                process_task.cancel()
                lost_task.cancel()
                await asyncio.gather(process_task, lost_task, return_exceptions=True)
                raise
            if lock_lost.is_set():
                process_task.cancel()
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise self._lock_lost_error()
            lost_task.cancel()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return process_task.result()

    async def stream_message(
        self,
        user_id: str,
        message: str,
        *,
        request_id: str | None = None,
        attachment_ids: list[str] | None = None,
        retrieval_mode: str = "standard",
    ):
        """SSE 流式入口：与 process_message 相同的取锁顺序，持锁到流结束。

        生成器内部取锁（进程内锁 → 分布式锁 → 信号量），用 async for 转发
        instance.stream_message 的每个事件，finally 逆序释放。前端断连时
        asyncio.CancelledError 冒泡到生成器，finally 保证锁释放；同时
        instance.stream_message 自身的 try/finally 会 cancel 内层 request_task，
        断连后不再有无锁孤儿任务继续跑 LLM/写记忆。
        """
        # 取锁前懒初始化（与取锁分属不同锁段，避免 _per_user_lock 死锁）：
        # 跨 worker 场景当前 worker 可能没有该用户实例。
        instance = self.get(user_id)
        if not instance or not instance.initialized:
            await self.initialize_user(user_id)

        async with self._user_lock_scope(user_id) as lock_lost:
            # 锁已易主：不再继续输出（Important 3）
            if lock_lost.is_set():
                raise self._lock_lost_error()
            # 锁内重新获取实例；初始化后仍无实例则兜底报 NOT_INITIALIZED。
            instance = self.get(user_id)
            if not instance or not instance.initialized:
                from webui_new.core.errors import BusinessError
                raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")
            instance_kwargs = {
                "request_id": request_id,
                "attachment_ids": attachment_ids,
            }
            if retrieval_mode == "enhanced":
                instance_kwargs["retrieval_mode"] = "enhanced"
            stream = instance.stream_message(message, **instance_kwargs)
            iterator = stream.__aiter__()
            try:
                while True:
                    next_task = asyncio.create_task(iterator.__anext__())
                    lost_task = asyncio.create_task(lock_lost.wait())
                    try:
                        await asyncio.wait(
                            {next_task, lost_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if lock_lost.is_set():
                            next_task.cancel()
                            await asyncio.gather(next_task, return_exceptions=True)
                            raise self._lock_lost_error()
                        lost_task.cancel()
                        await asyncio.gather(lost_task, return_exceptions=True)
                        try:
                            event = next_task.result()
                        except StopAsyncIteration:
                            break
                        yield event
                    except BaseException:
                        next_task.cancel()
                        lost_task.cancel()
                        await asyncio.gather(next_task, lost_task, return_exceptions=True)
                        raise
            finally:
                aclose = getattr(iterator, "aclose", None)
                if callable(aclose):
                    await aclose()

    def get_status(self, user_id: str) -> dict:
        instance = self.get(user_id)
        if not instance:
            return {"initialized": False}
        return {
            "initialized": instance.initialized,
            "error": instance.init_error,
        }
