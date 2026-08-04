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
from typing import Optional

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from agents.intention_agent import IntentionAgent
from agents.orchestration_agent import OrchestrationAgent
from settings import (
    MEMORY_CONFIG,
    ORCHESTRATION_V2_CONFIG,
    RESILIENCE_CONFIG,
    TRIP_INTAKE_CONFIG,
)
from context.memory_manager import MemoryManager
from runtime import create_agent_runtime, create_circuit_breaker
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.llm_resilience import retry_with_backoff
from utils.logging_safety import sanitize_for_log
from utils.memory_safety import redact_sensitive_text, wrap_untrusted_memory
from utils.observability import COMPONENT_LLM, ERROR_CIRCUIT_OPEN, record_upstream_error
from webui_new.core.errors import BusinessError, InternalError, UpstreamError
from core.onboarding import InitialPreferenceOnboarding
from core.intent_router import FastIntentRouter
from core.intent_catalog import INTENT_DISPLAY_NAMES
from core.orchestration.pipeline import MultiIntentPipeline
from core.orchestration.validator import supports_phase_one
from core.presentation import build_legacy_answer_document, build_trip_intake_document
from core.execution_budget import (
    ExecutionBudget,
    ExecutionLimitExceeded,
    consume_agent_call,
    execution_budget_scope,
)

logger = logging.getLogger(__name__)

# 智能体显示名称（统一来源：core.intent_catalog）
AGENT_DISPLAY_NAMES = INTENT_DISPLAY_NAMES


class HommeyWebInstance:
    """单个用户的 Hommey 实例"""

    # 简单闲聊匹配规则（不经过 LLM）
    CHITCHAT_PATTERNS = [
        "你好", "您好", "嗨", "hi", "hello", "hey",
        "谢谢", "感谢", "多谢", "thanks", "thank",
        "再见", "拜拜", "bye", "回头见",
        "在吗", "在不在", "有人吗",
        "哈哈", "呵呵", "好的", "ok", "okay",
        "没事", "没什么", "算了",
        "再见", "明天见", "下次见",
        "你叫什么", "你是谁", "你能做什么",
    ]

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.session_id = str(uuid.uuid4())[:8]
        self.memory_manager: Optional[MemoryManager] = None
        self.orchestrator: Optional[OrchestrationAgent] = None
        self.intention_agent: Optional[IntentionAgent] = None
        self.attachment_service = None  # 多模态附件服务（runtime 注入；详见方案 §4.5）
        self.model = None
        self.multi_intent_pipeline: Optional[MultiIntentPipeline] = None
        self._agent_cache = {}
        self.circuit_breaker: Optional[CircuitBreaker] = None
        self.onboarding = InitialPreferenceOnboarding()
        self.initialized = False
        self.init_error: Optional[str] = None

        # ── 性能优化: 缓存 ──
        self._summary_cache: Optional[str] = None  # 长期记忆摘要缓存
        self._summary_msg_count: int = 0           # 缓存时的消息数
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
            self.intention_agent = runtime.intention_agent
            self.orchestrator = runtime.orchestrator
            self.multi_intent_pipeline = MultiIntentPipeline(
                model=self.model,
                composer_model=runtime.composer_model,
                agent_runner=self.orchestrator.execute_task,
            )
            self.attachment_service = getattr(runtime, "attachment_service", None)
            self._agent_cache = runtime.agent_cache
            self.circuit_breaker = create_circuit_breaker()

            self.initialized = True
        except Exception as e:
            self.init_error = "初始化失败，请稍后刷新页面重试"
            logger.error("Init failed for user %s: %s", self.user_id, sanitize_for_log(e))
            raise

    async def get_preferences(self) -> dict:
        """获取用户偏好"""
        if not self.memory_manager:
            return {"preferences": [], "raw": {}}
        prefs = self.memory_manager.long_term.get_preference()
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
        return self.onboarding.needs_onboarding(self.memory_manager)

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
        titles = self.memory_manager.long_term.get_chat_session_titles()
        return {
            "session_id": session_id,
            "title": titles.get(session_id),
            "messages": self._with_attachments(rows),
        }

    def _with_attachments(self, rows: list[dict]) -> list[dict]:
        """给历史消息附带其绑定的附件清单（供前端渲染附件卡片）。仅在附件服务可用时生效。"""
        if not self.attachment_service or not rows:
            return rows
        repository = getattr(self.attachment_service, "repository", None)
        if repository is None:
            return rows
        enriched = []
        for row in rows:
            message_id = row.get("id")
            if row.get("role") == "user" and message_id is not None and not isinstance(message_id, bool):
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
        session_id = str(uuid.uuid4())[:8]
        self.session_id = session_id
        if self.memory_manager:
            self.memory_manager.rotate_session(session_id)
        self._last_activity_monotonic = None
        self._total_messages = 0
        return session_id

    def activate_chat_session(self, session_id: str) -> dict:
        payload = self.get_chat_session(session_id)
        if not payload["messages"]:
            raise ValueError("Chat session not found")
        self.session_id = session_id
        if self.memory_manager:
            self.memory_manager.rotate_session(session_id)
            for message in payload["messages"][-10:]:
                role = message.get("role")
                content = message.get("content")
                if role in {"user", "assistant"} and content:
                    self.memory_manager.short_term.add_message(role, content)
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
        return self.onboarding.get_state(self.memory_manager)

    async def save_onboarding_preference(self, key: str, value: str) -> dict:
        """Save one first-run preference without using the chat pipeline."""
        if not self.memory_manager:
            raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")
        return self.onboarding.save_answer(self.memory_manager, key, value)

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
        return {"active_trip": self.memory_manager.get_active_trip()}

    @staticmethod
    def _is_simple_chitchat(message: str) -> bool:
        """快速判断是否纯闲聊（不经过 LLM）"""
        msg = message.strip().lower()
        # 纯问候/感谢/告别
        for pattern in HommeyWebInstance.CHITCHAT_PATTERNS:
            if msg == pattern or msg.startswith(pattern) and len(msg) < 15:
                return True
        # 单字/简单表情
        if len(msg) <= 2 and msg in ("嗯", "哦", "啊", "好", "行", "ok"):
            return True
        return False

    def _normalize_input(
        self, message: str, attachment_ids: list[str] | None
    ) -> tuple[str, str]:
        """方案 §4.5：把附件规范化为 agent_query（喂 LLM）与 display_message（写记忆）。

        无附件或附件服务未注入时，agent_query == display_message == message。
        """
        if attachment_ids and self.attachment_service is not None:
            try:
                normalized = self.attachment_service.normalize(message, attachment_ids, self.user_id)
                return normalized.agent_query, normalized.display_message
            except AppError:
                raise
            except Exception as e:
                logger.warning("附件规范化失败，回退为纯文本: %s", sanitize_for_log(e))
        return message, message

    async def _get_cached_summary(self) -> str:
        """Cache only query-independent memory; dynamic trip retrieval stays per request."""
        stats = self.memory_manager.short_term.get_statistics()
        current_count = int(stats.get("message_version", stats.get("total_messages", 0)))

        # 仅在首次或消息数增长超过阈值时重新生成
        if self._summary_cache is None or current_count - self._summary_msg_count >= 5:
            summary = await self._get_long_term_summary()
            if summary:
                self._summary_cache = summary
                self._summary_msg_count = current_count
                return summary
            elif self._summary_cache is not None:
                self._summary_msg_count = current_count
                return self._summary_cache
            self._summary_cache = ""
            self._summary_msg_count = current_count
            return ""

        return self._summary_cache or ""

    def _ensure_active_session(self) -> bool:
        """Rotate the dialogue session after the configured idle timeout."""
        now = time.monotonic()
        timeout = int(MEMORY_CONFIG.get("short_term", {}).get("session_idle_timeout_sec", 600))
        rotated = bool(
            self._last_activity_monotonic is not None
            and now - self._last_activity_monotonic >= max(timeout, 1)
        )
        if rotated:
            self.session_id = str(uuid.uuid4())[:8]
            self.memory_manager.rotate_session(self.session_id)
            self._summary_cache = None
            self._summary_msg_count = 0
            self._total_messages = 0
        self._last_activity_monotonic = now
        return rotated

    def _handle_task_lifecycle_command(self, message: str) -> Optional[str]:
        """Handle explicit, narrowly-scoped current-task completion/cancellation commands."""
        normalized = "".join(message.strip().lower().split())
        cancel_commands = {
            "取消当前行程", "取消这个行程", "这个行程取消", "这个行程不安排了", "不安排这个行程了",
        }
        complete_commands = {
            "完成当前行程", "结束当前行程", "当前行程完成了", "这个行程完成了", "行程规划完成",
        }
        if normalized in cancel_commands:
            cancelled = self.memory_manager.cancel_active_trip()
            return "已取消当前行程任务。" if cancelled else "当前没有进行中的行程任务。"
        if normalized in complete_commands:
            completed = self.memory_manager.complete_active_trip(reason="user_completed")
            return "已结束当前行程任务。" if completed else "当前没有进行中的行程任务。"
        return None

    async def process_message(
        self,
        message: str,
        request_id: str | None = None,
        attachment_ids: list[str] | None = None,
        progress_callback=None,
    ) -> dict:
        """Run one user request inside an isolated execution budget and deadline."""
        rc = RESILIENCE_CONFIG
        budget = ExecutionBudget(
            max_agent_calls=rc.get("max_agent_calls_per_request", 8),
            max_external_calls=rc.get("max_external_calls_per_request", 16),
            max_external_calls_per_type=rc.get("max_external_calls_per_type", 6),
        )
        try:
            with execution_budget_scope(budget):
                implementation_kwargs = {
                    "request_id": request_id,
                    "attachment_ids": attachment_ids,
                }
                if progress_callback is not None:
                    implementation_kwargs["progress_callback"] = progress_callback
                return await asyncio.wait_for(
                    self._process_message_impl(message, **implementation_kwargs),
                    timeout=rc.get("request_timeout_sec", 120.0),
                )
        except ExecutionLimitExceeded as exc:
            raise UpstreamError(
                exc.code,
                exc.public_message,
                retryable=False,
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
            logger.info("Request execution budget user_id=%s budget=%s", self.user_id, budget.snapshot())

    async def _process_message_impl(
        self,
        message: str,
        request_id: str | None = None,
        attachment_ids: list[str] | None = None,
        progress_callback=None,
    ) -> dict:
        """处理用户消息，返回响应"""
        from agentscope.message import Msg

        start_time = time.perf_counter()
        timings = {}

        if not self.initialized:
            raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")

        self._ensure_active_session()
        if request_id:
            get_recorded_response = getattr(self.memory_manager, "get_recorded_response", None)
            recorded = get_recorded_response(request_id) if get_recorded_response else None
            if recorded:
                get_recorded_document = getattr(
                    self.memory_manager, "get_recorded_answer_document", None
                )
                get_recorded_presentation = getattr(
                    self.memory_manager, "get_recorded_presentation_document", None
                )
                recorded_document = (
                    get_recorded_document(request_id) if get_recorded_document else None
                )
                recorded_presentation = (
                    get_recorded_presentation(request_id) if get_recorded_presentation else None
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

        # 多模态输入规范化（方案 §4.5）：附件只进 agent_query，display_message 进记忆。
        agent_query, display_message = self._normalize_input(message, attachment_ids)

        lifecycle_response = self._handle_task_lifecycle_command(message)
        if lifecycle_response:
            self.memory_manager.add_message("user", display_message, metadata)
            self.memory_manager.add_message("assistant", lifecycle_response, metadata)
            return {"response": lifecycle_response, "agents": [], "preferences_updated": False}

        # ═══ 优化 1: 简单闲聊直接处理，不经过 LLM ═══
        # 带附件时不走闲聊短路，避免附件问题被草率打发。
        if self._is_simple_chitchat(message) and not attachment_ids:
            self.memory_manager.add_message("user", display_message, metadata)
            response = await self._handle_chitchat(message)
            self.memory_manager.add_message("assistant", response, metadata)
            return {"response": response, "agents": [], "preferences_updated": False}

        rc = RESILIENCE_CONFIG
        agent_max_retries = rc.get("agent_max_retries", 1)
        # 带附件时强制走完整意图链路（_build_context 会把 agent_query 含附件上下文喂给 LLM），
        # 不走绕过上下文的 fast_route，避免附件文本无法到达模型。
        fast_route = None if attachment_ids else self._route_without_context(message)

        if fast_route:
            intention_data = fast_route.to_intention_data(agent_query)
            intention_result = Msg(
                name="IntentionAgent",
                content=json.dumps(intention_data, ensure_ascii=False),
                role="assistant",
            )
            timings["context"] = 0.0
            timings["intent"] = 0.0
        else:
            # ═══ 优化 2: 缓存长期记忆摘要，避免每次都 LLM 总结 ═══
            # 同时构建上下文和意图识别可以部分重叠
            context_future = asyncio.ensure_future(self._build_context(agent_query))

            # 2. Intent recognition
            try:
                if self.circuit_breaker:
                    self.circuit_breaker.raise_if_open()

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
                    self.circuit_breaker.record_success()
            except ExecutionLimitExceeded:
                raise
            except CircuitOpenError:
                record_upstream_error(COMPONENT_LLM, ERROR_CIRCUIT_OPEN, retryable=True)
                raise UpstreamError("CIRCUIT_OPEN", "服务暂时不可用，请稍后再试。", retryable=True, component=COMPONENT_LLM)
            except Exception as e:
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure()
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

        self._total_messages += 1
        message_id = self.memory_manager.add_message("user", display_message, metadata)
        # 把附件绑定到这条用户消息（方案 §4.4）；记忆只存 display_message，附件全文不进库。
        if attachment_ids and self.attachment_service is not None and message_id is not False and message_id:
            try:
                self.attachment_service.bind(message_id, attachment_ids, self.user_id)
            except Exception as e:
                logger.warning("附件绑定失败 request_id=%s: %s", request_id, sanitize_for_log(e))

        use_task_pipeline = bool(
            ORCHESTRATION_V2_CONFIG.get("enabled", True)
            and self.multi_intent_pipeline is not None
            and supports_phase_one(intention_data)
        )
        if use_task_pipeline:
            try:
                orchestration_start = time.perf_counter()
                pipeline_output = await self.multi_intent_pipeline.run(
                    original_query=display_message,
                    intention_data=intention_data,
                    base_context=self.orchestrator.prepare_context(intention_data),
                    progress=progress_callback,
                )
                timings["orchestration"] = time.perf_counter() - orchestration_start
                self.orchestrator.record_task_results(intention_data, pipeline_output.results)
                if self.circuit_breaker:
                    self.circuit_breaker.record_success()
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
                    self.circuit_breaker.record_failure()
                logger.error("Task orchestration failed: %s", sanitize_for_log(e))
                record_upstream_error(COMPONENT_LLM, e, retryable=True)
                raise UpstreamError(
                    "ORCHESTRATION_FAILED",
                    "调度执行失败，请稍后重试。",
                    retryable=True,
                    component=COMPONENT_LLM,
                    debug_message=str(e),
                )
            answer_document = pipeline_output.answer_document.model_dump(mode="json")
            response = pipeline_output.answer_document.plain_text
            assistant_metadata = dict(metadata)
            assistant_metadata["answer_document"] = answer_document
            self.memory_manager.add_message("assistant", response, assistant_metadata)
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
                "preferences_updated": False,
                "timings": {key: round(value, 3) for key, value in timings.items()},
            }

        # 3. Legacy orchestration for single intents and dependent workflows.
        try:
            orchestration_start = time.perf_counter()
            progress_reply = getattr(self.orchestrator, "reply_with_progress", None)
            if progress_callback is None or progress_reply is None:
                orchestration_result = await self.orchestrator.reply(intention_result)
            else:
                orchestration_result = await progress_reply(
                    intention_result,
                    progress_callback,
                )
            timings["orchestration"] = time.perf_counter() - orchestration_start
            if self.circuit_breaker:
                self.circuit_breaker.record_success()
        except ExecutionLimitExceeded:
            raise
        except CircuitOpenError:
            record_upstream_error(COMPONENT_LLM, ERROR_CIRCUIT_OPEN, retryable=True)
            raise UpstreamError("CIRCUIT_OPEN", "服务暂时不可用，请稍后再试。", retryable=True, component=COMPONENT_LLM)
        except Exception as e:
            if self.circuit_breaker:
                self.circuit_breaker.record_failure()
            logger.error("Orchestration failed: %s", sanitize_for_log(e))
            record_upstream_error(COMPONENT_LLM, e, retryable=True)
            raise UpstreamError(
                "ORCHESTRATION_FAILED",
                "调度执行失败，请稍后重试。",
                retryable=True,
                component=COMPONENT_LLM,
                debug_message=str(e),
            )

        try:
            result_data = json.loads(orchestration_result.content)
        except json.JSONDecodeError:
            raise InternalError("ORCHESTRATION_PARSE_FAILED", "解析结果失败，请稍后重试")
        self._raise_on_agent_errors(result_data)

        # 4. Chitchat fallback
        if result_data.get("status") == "no_agents" and not result_data.get("results"):
            if result_data.get("message"):
                response = result_data["message"]
                self.memory_manager.add_message("assistant", response, metadata)
                return {"response": response, "agents": [], "preferences_updated": False}
            response = await self._handle_chitchat(agent_query)
            self.memory_manager.add_message("assistant", response, metadata)
            return {"response": response, "agents": [], "preferences_updated": False}

        # 5. Format response. Incomplete trip intake uses a typed presentation
        # document; plain text remains available for old clients and exports.
        presentation_document = self._trip_intake_presentation(result_data)
        answer_document = None
        if presentation_document:
            response = presentation_document["plain_text"]
            assistant_metadata = dict(metadata)
            assistant_metadata["presentation_document"] = presentation_document
            self.memory_manager.add_message("assistant", response, assistant_metadata)
        else:
            response = self._format_response(result_data)
            document = build_legacy_answer_document(result_data, response)
            if document:
                answer_document = document.model_dump(mode="json")
                assistant_metadata = dict(metadata)
                assistant_metadata["answer_document"] = answer_document
                self.memory_manager.add_message("assistant", response, assistant_metadata)
            else:
                self.memory_manager.add_message("assistant", response, metadata)

        # 6. Extract agent names
        agents = []
        results = result_data.get("results", [])
        for r in results:
            name = r.get("agent_name", "")
            status = r.get("status", "")
            agents.append({
                "name": name,
                "display": AGENT_DISPLAY_NAMES.get(name, name),
                "status": status,
                "duration_sec": r.get("duration_sec"),
            })

        # 7. Check if preferences were updated
        prefs_updated = any(r.get("agent_name") == "preference" and r.get("status") == "success" for r in results)
        timings["total"] = time.perf_counter() - start_time
        logger.info(
            "WebUI message timing for %s: %s",
            self.user_id,
            {key: round(value, 3) for key, value in timings.items()},
        )

        return {
            "response": response,
            "answer_document": answer_document,
            "presentation_document": presentation_document,
            "agents": agents,
            "preferences_updated": prefs_updated,
            "timings": {key: round(value, 3) for key, value in timings.items()},
        }

    @staticmethod
    def _trip_intake_presentation(result_data: dict) -> dict | None:
        """Build a structured prompt when planning is paused for required facts."""
        if not TRIP_INTAKE_CONFIG.get("enabled", True):
            return None
        for result in result_data.get("results", []):
            if result.get("agent_name") != "event_collection" or result.get("status") != "success":
                continue
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            nested = data.get("data") if isinstance(data.get("data"), dict) else data
            if nested.get("planning_ready") is False:
                return build_trip_intake_document(nested).model_dump(mode="json")
        return None

    def _raise_on_agent_errors(self, result_data: dict) -> None:
        """Convert internal agent error payloads into the public AppError flow."""
        overall_status = result_data.get("status")
        if overall_status in {"completed", "partial_failure", "no_agents"}:
            return

        errors = []
        for result in result_data.get("results", []):
            agent_name = result.get("agent_name", "unknown")
            status = result.get("status")
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            message = (
                result.get("error_message")
                or result.get("message")
                or data.get("error")
                or data.get("message")
            )

            if status == "error":
                errors.append({
                    "agent_name": agent_name,
                    "message": message or "agent returned error status",
                    "error_code": result.get("error_code") or "AGENT_EXECUTION_FAILED",
                })
                continue

            if status == "success" and data.get("error") and not self._has_agent_success_payload(agent_name, data):
                errors.append({
                    "agent_name": agent_name,
                    "message": data.get("error"),
                    "error_code": "AGENT_EXECUTION_FAILED",
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

    @staticmethod
    def _has_agent_success_payload(agent_name: str, data: dict) -> bool:
        """Best-effort guard for legacy agents that may include non-fatal error fields."""
        if agent_name == "information_query":
            results = data.get("results") if isinstance(data.get("results"), dict) else data
            return bool(results.get("summary") or results.get("message"))
        if agent_name == "rag_knowledge":
            return bool(data.get("answer") or data.get("content") or data.get("data", {}).get("answer"))
        return any(data.get(key) for key in ("answer", "content", "result", "message", "summary", "itinerary", "preferences"))

    async def stream_message(
        self,
        message: str,
        request_id: str | None = None,
        attachment_ids: list[str] | None = None,
    ):
        """Yield live orchestration progress and response events as NDJSON payloads."""
        queue: asyncio.Queue = asyncio.Queue()
        result_holder = {}

        async def progress_callback(event):
            payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else dict(event)
            await queue.put(payload)

        async def run_request():
            try:
                result_holder["result"] = await self.process_message(
                    message,
                    request_id=request_id,
                    attachment_ids=attachment_ids,
                    progress_callback=progress_callback,
                )
            finally:
                await queue.put(None)

        yield {"type": "status", "phase": "analyzing", "message_key": "request_analyzing"}
        request_task = asyncio.create_task(run_request())
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
        await request_task
        result = result_holder["result"]

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
        if not route or len(route.agent_schedule) != 1:
            return None
        agent_name = route.agent_schedule[0].get("agent_name")
        if agent_name in {"rag_knowledge", "information_query", "chitchat"}:
            return route
        return None

    async def _build_context(self, message: str) -> list:
        """构建上下文消息（可与其他异步任务并行）"""
        from agentscope.message import Msg

        long_term_summary = await self._get_cached_summary()
        relevant_trip_context = self._get_relevant_trip_context(message)
        recent_context = self.memory_manager.short_term.get_recent_context(n_turns=5)

        context_messages = []
        active_trip = self.memory_manager.get_active_trip()
        memory_parts = []
        if active_trip:
            memory_parts.extend(["【当前出差任务】", json.dumps(active_trip, ensure_ascii=False)])
        if long_term_summary:
            memory_parts.append(long_term_summary)
        if relevant_trip_context:
            memory_parts.append(relevant_trip_context)
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

    async def _get_long_term_summary(self) -> str:
        """Generate query-independent profile and historical-session summary."""
        summary_parts = []
        prefs = self.memory_manager.long_term.get_preference()
        if prefs:
            pref_lines = ["【用户背景信息】（来自长期记忆）"]
            for pref_key, pref_value in prefs.items():
                if pref_value:
                    if isinstance(pref_value, list):
                        pref_lines.append(f"• {pref_key}: {', '.join(pref_value)}")
                    else:
                        pref_lines.append(f"• {pref_key}: {pref_value}")
            if len(pref_lines) > 1:
                summary_parts.extend(pref_lines)

        chat_summary = await self.memory_manager.get_long_term_summary_async(max_messages=20)
        if chat_summary:
            summary_parts.append("\n【历史会话总结】")
            summary_parts.append(chat_summary)

        return "\n".join(summary_parts) if summary_parts else ""

    def _get_relevant_trip_context(self, user_input: str) -> str:
        """Select recent and query-relevant trips without contaminating the static cache."""
        summary_parts = []
        all_trips = self.memory_manager.long_term.get_trip_history(limit=None)
        if all_trips:
            all_trips = sorted(
                all_trips,
                key=lambda item: item.get("timestamp", "") or "",
                reverse=True,
            )
            relevant_trips = []
            other_trips = []
            for trip in all_trips:
                origin = trip.get("origin", "") or ""
                destination = trip.get("destination", "") or ""
                if (origin and origin in user_input) or (destination and destination in user_input):
                    relevant_trips.append(trip)
                else:
                    other_trips.append(trip)
            trips_to_show = relevant_trips[:2] + other_trips[:1]
            if trips_to_show:
                summary_parts.append("\n【历史行程】")
                for i, trip in enumerate(trips_to_show[:3], 1):
                    origin = trip.get("origin", "未知")
                    destination = trip.get("destination", "未知")
                    start_date = trip.get("start_date", "")
                    purpose = trip.get("purpose", "")
                    mark = "✦ " if trip in relevant_trips else ""
                    summary_parts.append(f"{i}. {mark}{origin} → {destination} ({start_date}) - {purpose}")
            return "\n".join(summary_parts) if summary_parts else ""
        return ""

    async def _handle_chitchat(self, user_input: str) -> str:
        """闲聊兜底"""
        from agentscope.message import Msg
        import importlib.util

        agent = None
        try:
            agent = self.orchestrator.agent_registry["chitchat"]
        except (KeyError, Exception):
            pass

        if agent is None:
            try:
                script_path = os.path.join(
                    project_root, ".claude", "skills", "chitchat", "script", "agent.py"
                )
                spec = importlib.util.spec_from_file_location("chitchat_agent", script_path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    agent = module.ChitchatAgent(name="ChitchatAgent", model=self.model)
            except Exception as e:
                logger.warning(f"Failed to load ChitchatAgent: {e}")

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

    def _format_response(self, result_data: dict) -> str:
        """将智能体结果格式化为文本"""
        results = result_data.get("results", [])
        if not results:
            return "✓ 好的，我已收到。"
        lines = []
        planning_complete = any(
            item.get("agent_name") == "itinerary_planning" and item.get("status") == "success"
            for item in results
        )
        deferred_notes = []
        for result in results:
            agent_name = result.get("agent_name", "")
            status = result.get("status", "")
            data = result.get("data", {})
            if status == "error" and result.get("on_failure") == "continue":
                display = AGENT_DISPLAY_NAMES.get(agent_name, agent_name)
                deferred_notes.append(f"⚠️ {display}暂时不可用，已基于其余成功结果降级处理。")
                continue
            if planning_complete and agent_name in {"event_collection", "rag_knowledge", "information_query"}:
                continue
            if planning_complete and agent_name == "trip_compliance" and data.get("verdict") == "unknown":
                deferred_notes.append("📌 提醒：未检索到足以确认合规性的适用制度，请在提交前人工确认。")
                continue
            if status == "error":
                display = AGENT_DISPLAY_NAMES.get(agent_name, agent_name)
                lines.append(f"❌ {display}执行失败，请稍后重试。")
                continue
            if status != "success":
                continue
            text = self._format_agent_result(agent_name, data)
            if text:
                lines.append(text)
        return "\n".join(lines + deferred_notes).strip()

    def _format_agent_result(self, agent_name: str, data: dict) -> str:
        """格式化单个智能体输出"""
        # Itinerary
        if agent_name == "itinerary_planning":
            itinerary = data.get("itinerary")
            if not itinerary and "data" in data and isinstance(data["data"], dict):
                itinerary = data["data"].get("itinerary")
            if itinerary:
                parts = [f"✈️ **{itinerary.get('title', '行程规划')}**"]
                parts.append(f"  时长: {itinerary.get('duration', '未知')}\n")
                transport = itinerary.get("transport_recommendation")
                if isinstance(transport, dict) and transport:
                    parts.append("🚄 **交通建议**")
                    if transport.get("preferred"):
                        parts.append(f"  • 首选: {transport['preferred']}")
                    if transport.get("reason"):
                        parts.append(f"  • 原因: {transport['reason']}")
                    if transport.get("alternative"):
                        parts.append(f"  • 备选: {transport['alternative']}")
                    if transport.get("verification"):
                        parts.append(f"  • 核验: {transport['verification']}")
                    parts.append("")
                elif isinstance(transport, str) and transport:
                    parts.extend(["🚄 **交通建议**", f"  • {transport}", ""])

                lodging = itinerary.get("lodging_advice")
                if lodging:
                    parts.extend(["🏨 **住宿建议**", f"  • {lodging}", ""])

                for day_plan in itinerary.get("daily_plans", []):
                    day_num = day_plan.get("day", 1)
                    parts.append(f"**第 {day_num} 天**")
                    activities = day_plan.get("activities") or day_plan.get("time_slots") or []
                    for slot in activities:
                        time = slot.get("time", "")
                        activity = slot.get("activity") or slot.get("location") or ""
                        description = slot.get("description", "")
                        transport = slot.get("transport", "")
                        parts.append(f"  {time} - {activity}")
                        if description:
                            parts.append(f"  _{description}_")
                        if transport:
                            parts.append(f"  🚇 {transport}")
                    meals = day_plan.get("meals", {})
                    if meals:
                        if meals.get("lunch"):
                            parts.append(f"  🍜 {meals['lunch']}")
                        if meals.get("dinner"):
                            parts.append(f"  🍽️ {meals['dinner']}")
                    parts.append("")
                notes = itinerary.get("notes", [])
                if notes:
                    parts.append("📌 **注意事项**")
                    for note in notes:
                        parts.append(f"  • {note}")
                checklist = itinerary.get("reimbursement_checklist", [])
                if checklist:
                    parts.append("🧾 **报销准备**")
                    for item in checklist:
                        parts.append(f"  • {item}")
                budget = itinerary.get("estimated_budget")
                if budget:
                    parts.append(f"💰 **预算参考**: {budget}")
                missing_info = itinerary.get("missing_info", [])
                if missing_info:
                    parts.append(f"💡 **待补充**: {', '.join(missing_info)}")
                return "\n".join(parts)

        # Preference
        if agent_name == "preference":
            raw_prefs = data.get("preferences")
            if not raw_prefs and "data" in data and isinstance(data["data"], dict):
                raw_prefs = data["data"].get("preferences")
            if isinstance(raw_prefs, dict):
                prefs_list = raw_prefs.get("preferences", [])
            else:
                prefs_list = raw_prefs if isinstance(raw_prefs, list) else []
            if prefs_list:
                parts = ["✓ **已更新您的偏好设置**"]
                type_names = {
                    "home_location": "常驻地",
                    "transportation_preference": "交通偏好",
                    "hotel_brands": "酒店偏好",
                    "airlines": "航空公司偏好",
                    "seat_preference": "座位偏好",
                    "meal_preference": "餐食偏好",
                    "budget_level": "预算等级",
                }
                for pref in prefs_list:
                    pref_type = pref.get("type", "")
                    pref_value = pref.get("value", "")
                    action = pref.get("action", "replace")
                    display_type = type_names.get(pref_type, pref_type)
                    action_text = "追加" if action == "append" else "设置为"
                    parts.append(f"  • {display_type} {action_text} `{pref_value}`")
                return "\n".join(parts)

        # Event collection
        if agent_name == "event_collection":
            origin = data.get("origin") or data.get("data", {}).get("origin")
            destination = data.get("destination") or data.get("data", {}).get("destination")
            start_date = data.get("start_date") or data.get("data", {}).get("start_date")
            end_date = data.get("end_date") or data.get("data", {}).get("end_date")
            missing_info = data.get("missing_info") or data.get("data", {}).get("missing_info") or []
            optional_info = data.get("optional_info") or data.get("data", {}).get("optional_info") or []
            field_labels = {
                "origin": "出发地",
                "destination": "目的地",
                "start_date": "出发日期（如：7月14日）",
                "end_date": "返程日期",
                "duration_days": "出差天数",
                "duration_days_or_end_date": "出差天数或返程日期",
                "trip_purpose": "出差目的（如：拜访客户、参加会议）",
                "work_location": "客户/会议地点",
                "work_schedule": "会面或工作时间",
            }
            missing_labels = [field_labels.get(item, str(item)) for item in missing_info]
            optional_labels = [field_labels.get(item, str(item)) for item in optional_info]
            parts = []
            if destination or origin:
                parts.append("✓ **已收集行程信息**")
                if origin:
                    parts.append(f"  • 出发地: `{origin}`")
                if destination:
                    parts.append(f"  • 目的地: `{destination}`")
                if start_date:
                    parts.append(f"  • 出发日期: `{start_date}`")
                if end_date:
                    parts.append(f"  • 返程日期: `{end_date}`")
            if missing_labels:
                parts.append(f"💡 为开始生成行程，请补充：{'；'.join(missing_labels)}")
            if optional_labels:
                parts.append(f"可选补充（有助于优化安排）：{'；'.join(optional_labels)}")
            return "\n".join(parts) if parts else ""

        # Information query
        if agent_name == "information_query":
            query_results = data.get("results")
            if not query_results and "data" in data and isinstance(data["data"], dict):
                query_results = data["data"].get("results")
            if not query_results:
                query_results = data
            if not isinstance(query_results, dict):
                query_results = {}
            summary = query_results.get("summary", "")
            message = query_results.get("message", "")
            error = query_results.get("error", "")
            parts = []
            if summary:
                parts.append(summary)
            elif message:
                parts.append(message)
            elif error:
                parts.append(error)
            return "\n".join(parts) if parts else ""

        # RAG knowledge
        if agent_name == "rag_knowledge":
            answer = data.get("answer")
            if not answer and "data" in data and isinstance(data["data"], dict):
                answer = data["data"].get("answer")
            if not answer:
                answer = data.get("content") or data.get("data", {}).get("content")
            if isinstance(answer, dict):
                answer = answer.get("answer", str(answer))
            if isinstance(answer, str) and answer.strip().startswith("{") and answer.strip().endswith("}"):
                try:
                    json_obj = json.loads(answer)
                    if isinstance(json_obj, dict) and "answer" in json_obj:
                        answer = json_obj["answer"]
                except Exception:
                    pass
            if answer:
                parts = [str(answer)]
                sources = data.get("sources") or data.get("data", {}).get("sources") or []
                if sources:
                    parts.append("\n📚 **制度来源**")
                    seen = set()
                    for source in sources:
                        if not isinstance(source, dict):
                            continue
                        location = " · ".join(
                            str(value) for value in (
                                source.get("file"),
                                source.get("section"),
                                source.get("page") and f"第{source['page']}页",
                            ) if value
                        )
                        if location and location not in seen:
                            seen.add(location)
                            parts.append(f"  • {location}")
                return "\n".join(parts)

        # Memory query
        if agent_name == "memory_query":
            query_result = data.get("answer") or data.get("result") or data.get("content")
            if not query_result and "data" in data and isinstance(data["data"], dict):
                inner = data["data"]
                query_result = inner.get("answer") or inner.get("result") or inner.get("content")
            if query_result:
                return str(query_result)

        # Chitchat
        if agent_name == "chitchat":
            response = data.get("response") or data.get("data", {}).get("response")
            if isinstance(response, dict):
                response = response.get("response", str(response))
            if response:
                return str(response)

        if agent_name == "trip_compliance":
            verdict = data.get("verdict", "unknown")
            labels = {
                "compliant": "符合制度",
                "non_compliant": "存在不合规项",
                "partial": "部分项目待确认",
                "unknown": "暂时无法确认",
            }
            parts = [f"🛡️ **合规检查：{labels.get(verdict, verdict)}**"]
            if data.get("summary"):
                parts.append(str(data["summary"]))
            for check in data.get("checks", []):
                if isinstance(check, dict):
                    parts.append(f"  • {check.get('item', '检查项')}: {check.get('status', 'unknown')} — {check.get('reason', '')}")
            if data.get("unknown_items"):
                parts.append("📌 **待确认**")
                parts.extend(f"  • {item}" for item in data["unknown_items"])
            sources = data.get("sources") or []
            if sources:
                parts.append("📚 **制度来源**")
                for source in sources:
                    if not isinstance(source, dict):
                        continue
                    location = " · ".join(str(value) for value in (source.get("file"), source.get("section"), source.get("page") and f"第{source['page']}页") if value)
                    parts.append(f"  • {location}")
            return "\n".join(parts)

        # Fallback
        if data:
            common_keys = ["answer", "content", "result", "message", "summary", "text", "description"]
            for k in common_keys:
                if k in data and isinstance(data[k], str) and data[k].strip():
                    return data[k]
                if "data" in data and isinstance(data["data"], dict):
                    if k in data["data"] and isinstance(data["data"][k], str) and data["data"][k].strip():
                        return data["data"][k]

        display = AGENT_DISPLAY_NAMES.get(agent_name, agent_name)
        return f"✓ {display}已完成"


class WebHommeyManager:
    """管理所有用户的 Hommey 实例"""

    def __init__(self):
        self._instances: dict[str, HommeyWebInstance] = {}

    def get_or_create(self, user_id: str) -> HommeyWebInstance:
        if user_id not in self._instances:
            self._instances[user_id] = HommeyWebInstance(user_id)
        return self._instances[user_id]

    def get(self, user_id: str) -> Optional[HommeyWebInstance]:
        return self._instances.get(user_id)

    async def initialize_user(self, user_id: str) -> HommeyWebInstance:
        instance = self.get_or_create(user_id)
        if not instance.initialized:
            await instance.initialize()
        return instance

    def get_status(self, user_id: str) -> dict:
        instance = self.get(user_id)
        if not instance:
            return {"initialized": False}
        return {
            "initialized": instance.initialized,
            "error": instance.init_error,
        }
