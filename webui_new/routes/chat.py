"""
聊天 API 路由。

包含普通 chat 和 NDJSON stream 两个入口。路由层只做输入检查、调用
HommeyWebInstance，以及把异常转换成当前统一错误响应。
具体意图识别、编排、记忆更新等业务逻辑仍在 manager/agents 中。
"""
import json
import logging
import asyncio
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from utils.logging_safety import sanitize_for_log
from utils.memory_safety import redact_sensitive_text
from utils.observability import COMPONENT_HTTP, record_app_error, record_http_request
from webui_new.auth import User, get_current_user, require_path_user
from core.intent_catalog import intent_api_payload
from webui_new.core.errors import (
    AppError,
    BusinessError,
    InternalError,
    ValidationError,
    request_id,
    stream_error_event,
)
from webui_new.schemas.requests import ChatRequest, InterruptRequest, SessionRenameRequest
from webui_new.quick_trip import build_quick_trip_message
from core.integrations.places.amap import AMapError

logger = logging.getLogger(__name__)


def create_chat_router(manager, place_service=None):
    """创建聊天 router；manager 由 server.py 注入，避免反向 import server。"""
    router = APIRouter()

    @router.post("/api/{user_id}/chat")
    async def send_message(
        request: Request, user_id: str, data: ChatRequest, current_user: User = Depends(require_path_user)
    ):
        """发送消息并获取回复。

        不做 NOT_INITIALIZED 预检查：未初始化/跨 worker（实例在另一 worker）时
        由 manager.process_message 内部懒初始化。会话列表等不走统一入口的路由
        仍保留预检查。
        """
        if not data.message.strip() and not data.attachment_ids and data.trip_input is None:
            raise BusinessError("EMPTY_MESSAGE", "请输入消息或添加附件")

        try:
            message, structured_trip_input, _capability_selection = await _prepare_chat_input(
                data, place_service
            )
            rid = request_id(request)
            logger.info("[%s] ➤ %s", user_id, redact_sensitive_text(message))
            kwargs = {
                "request_id": rid,
                "attachment_ids": data.attachment_ids,
            }
            if structured_trip_input is not None:
                kwargs["structured_trip_input"] = structured_trip_input
            if data.session_id:
                kwargs["session_id"] = data.session_id
            if data.retrieval_mode == "enhanced":
                kwargs["retrieval_mode"] = "enhanced"
            result = await manager.process_message(user_id, message, **kwargs)
            safe_response = redact_sensitive_text(result.get("response", ""))
            logger.info("[%s] ◀ %s...", user_id, safe_response[:80])
            return result
        except AppError:
            raise
        except Exception as e:
            logger.error("Chat failed request_id=%s user_id=%s error=%s", request_id(request), user_id, sanitize_for_log(e))
            raise InternalError("CHAT_FAILED", "处理失败，请稍后重试")

    @router.post("/api/{user_id}/chat/stream")
    async def stream_message(
        request: Request, user_id: str, data: ChatRequest, current_user: User = Depends(require_path_user)
    ):
        """Stream chat progress and response chunks as newline-delimited JSON.

        不做 NOT_INITIALIZED 预检查：未初始化/跨 worker（实例在另一 worker）时
        由 manager.stream_message 内部懒初始化。
        """
        if not data.message.strip() and not data.attachment_ids and data.trip_input is None:
            raise BusinessError("EMPTY_MESSAGE", "请输入消息或添加附件")

        message, structured_trip_input, _capability_selection = await _prepare_chat_input(
            data, place_service
        )

        async def event_stream():
            """把 manager.stream_message() 的事件逐行编码为 NDJSON。

            manager.stream_message 在生成器内取锁（进程内锁 → 分布式锁 → 信号量），
            持锁到流结束；前端断连时 CancelledError 冒泡到生成器，finally 释放锁。
            """
            started_at = time.perf_counter()
            try:
                logger.info("[%s] -> %s", user_id, redact_sensitive_text(message))
                kwargs = {
                    "request_id": request_id(request),
                    "attachment_ids": data.attachment_ids,
                }
                if structured_trip_input is not None:
                    kwargs["structured_trip_input"] = structured_trip_input
                if data.session_id:
                    kwargs["session_id"] = data.session_id
                if data.retrieval_mode == "enhanced":
                    kwargs["retrieval_mode"] = "enhanced"
                async for event in manager.stream_message(user_id, message, **kwargs):
                    yield json.dumps(event, ensure_ascii=False) + "\n"
            except asyncio.CancelledError:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                record_app_error(COMPONENT_HTTP, "STREAM_CANCELLED", 499)
                record_http_request(request.url.path, request.method, 499, duration_ms)
                logger.warning(
                    "stream_cancelled",
                    extra={
                        "request_id": request_id(request),
                        "user_id": user_id,
                        "route": request.url.path,
                        "method": request.method,
                        "status_code": 499,
                        "error_code": "STREAM_CANCELLED",
                        "component": COMPONENT_HTTP,
                        "duration_ms": duration_ms,
                        "debug_message": "client disconnected while reading stream",
                    },
                )
                raise
            except Exception as e:
                rid = request_id(request)
                logger.error("Streaming chat failed request_id=%s user_id=%s error=%s", rid, user_id, sanitize_for_log(e))
                stream_exc = e if isinstance(e, AppError) else InternalError("STREAM_FAILED", "处理失败，请稍后重试")
                yield json.dumps(stream_error_event(request, stream_exc), ensure_ascii=False) + "\n"

        return StreamingResponse(
            event_stream(),
            media_type="application/x-ndjson; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @router.post("/api/{user_id}/orchestration/interrupt")
    async def interrupt_turn(
        user_id: str,
        data: InterruptRequest,
        current_user: User = Depends(require_path_user),
    ):
        return await manager.interrupt_active_turn(
            user_id, data.client_request_id, session_id=data.session_id,
        )

    @router.get("/api/{user_id}/sessions")
    async def list_sessions(user_id: str, current_user: User = Depends(require_path_user)):
        return await manager.run_user_state_operation(
            user_id,
            lambda instance: {
                "active_session_id": instance.session_id,
                "sessions": instance.list_chat_sessions(),
            },
        )

    @router.post("/api/{user_id}/sessions")
    async def create_session(user_id: str, current_user: User = Depends(require_path_user)):
        session_id = await manager.run_user_state_operation(
            user_id, lambda instance: instance.start_new_chat_session()
        )
        return {"session_id": session_id}

    @router.get("/api/{user_id}/sessions/{session_id}")
    async def get_session(
        user_id: str,
        session_id: str,
        current_user: User = Depends(require_path_user),
    ):
        payload = await manager.run_user_state_operation(
            user_id, lambda instance: instance.get_chat_session(session_id)
        )
        if not payload["messages"]:
            raise BusinessError("SESSION_NOT_FOUND", "会话不存在或已被删除")
        return payload

    @router.post("/api/{user_id}/sessions/{session_id}/activate")
    async def activate_session(
        user_id: str,
        session_id: str,
        current_user: User = Depends(require_path_user),
    ):
        try:
            return await manager.run_user_state_operation(
                user_id, lambda instance: instance.activate_chat_session(session_id)
            )
        except ValueError:
            raise BusinessError("SESSION_NOT_FOUND", "会话不存在或已被删除")

    @router.patch("/api/{user_id}/sessions/{session_id}")
    async def rename_session(
        user_id: str,
        session_id: str,
        data: SessionRenameRequest,
        current_user: User = Depends(require_path_user),
    ):
        title = data.title.strip()
        if not title:
            raise ValidationError("EMPTY_SESSION_TITLE", "会话名称不能为空")
        try:
            await manager.run_user_state_operation(
                user_id, lambda instance: instance.rename_chat_session(session_id, title)
            )
        except ValueError:
            raise BusinessError("SESSION_NOT_FOUND", "会话不存在或已被删除")
        return {"session_id": session_id, "title": title[:80]}

    @router.delete("/api/{user_id}/sessions/{session_id}")
    async def delete_session(
        user_id: str,
        session_id: str,
        current_user: User = Depends(require_path_user),
    ):
        active_session_id = await manager.run_user_state_operation(
            user_id, lambda instance: instance.delete_chat_session(session_id)
        )
        return {"active_session_id": active_session_id}

    @router.delete("/api/{user_id}/history")
    async def clear_chat_history(
        user_id: str,
        current_user: User = Depends(require_path_user),
    ):
        active_session_id = await manager.run_user_state_operation(
            user_id, lambda instance: instance.clear_chat_history()
        )
        return {"active_session_id": active_session_id}

    @router.get("/api/intents")
    async def list_intents(current_user: User = Depends(get_current_user)):
        """声明式意图目录：前端进度标签动态加载（app.js 本地 map 保底）。

        载荷 ``{intent: {display, progress_key, description, skill}}`` 由 skill
        目录派生；新增 skill 后无需改前端硬编码映射。
        """
        return intent_api_payload()

    return router


async def _prepare_chat_input(data: ChatRequest, place_service):
    """Verify provider-backed form locations and build the normal user utterance."""
    if data.input_source != "quick_trip_form" or data.trip_input is None:
        return data.message, None, None
    trip_input = data.trip_input.model_dump(mode="json")
    if trip_input.get("work_location_place_id"):
        if place_service is None or not place_service.configured:
            raise BusinessError("PLACE_SERVICE_NOT_CONFIGURED", "地点服务尚未配置，请联系管理员")
        try:
            verified = await place_service.verify(trip_input["work_location_place_id"])
        except AMapError as exc:
            raise BusinessError("PLACE_VERIFICATION_FAILED", "工作地点校验失败，请重新选择") from exc
        if verified is None:
            raise BusinessError("PLACE_NOT_FOUND", "工作地点已失效，请重新选择")
        trip_input["work_location"] = verified.name
        trip_input["work_location_verified"] = verified.model_dump(mode="json")
    selection = (
        data.capability_selection.model_dump(mode="json")
        if data.capability_selection is not None else {"include": ["nearby_hotels"], "exclude": []}
    )
    return build_quick_trip_message(trip_input, selection), trip_input, selection
