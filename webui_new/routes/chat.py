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
from webui_new.auth import User, require_path_user
from webui_new.core.errors import (
    AppError,
    BusinessError,
    InternalError,
    ValidationError,
    request_id,
    stream_error_event,
)
from webui_new.schemas.requests import ChatRequest, SessionRenameRequest

logger = logging.getLogger(__name__)


def create_chat_router(manager):
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
        if not data.message.strip() and not data.attachment_ids:
            raise BusinessError("EMPTY_MESSAGE", "请输入消息或添加附件")

        try:
            rid = request_id(request)
            logger.info("[%s] ➤ %s", user_id, redact_sensitive_text(data.message))
            result = await manager.process_message(
                user_id,
                data.message,
                request_id=rid,
                attachment_ids=data.attachment_ids,
            )
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
        if not data.message.strip() and not data.attachment_ids:
            raise BusinessError("EMPTY_MESSAGE", "请输入消息或添加附件")

        async def event_stream():
            """把 manager.stream_message() 的事件逐行编码为 NDJSON。

            manager.stream_message 在生成器内取锁（进程内锁 → 分布式锁 → 信号量），
            持锁到流结束；前端断连时 CancelledError 冒泡到生成器，finally 释放锁。
            """
            started_at = time.perf_counter()
            try:
                logger.info("[%s] -> %s", user_id, redact_sensitive_text(data.message))
                async for event in manager.stream_message(
                    user_id,
                    data.message,
                    request_id=request_id(request),
                    attachment_ids=data.attachment_ids,
                ):
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

    @router.get("/api/{user_id}/sessions")
    async def list_sessions(user_id: str, current_user: User = Depends(require_path_user)):
        instance = manager.get(user_id)
        if not instance or not instance.initialized:
            raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")
        return {
            "active_session_id": instance.session_id,
            "sessions": instance.list_chat_sessions(),
        }

    @router.post("/api/{user_id}/sessions")
    async def create_session(user_id: str, current_user: User = Depends(require_path_user)):
        instance = manager.get(user_id)
        if not instance or not instance.initialized:
            raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")
        return {"session_id": instance.start_new_chat_session()}

    @router.get("/api/{user_id}/sessions/{session_id}")
    async def get_session(
        user_id: str,
        session_id: str,
        current_user: User = Depends(require_path_user),
    ):
        instance = manager.get(user_id)
        if not instance or not instance.initialized:
            raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")
        payload = instance.get_chat_session(session_id)
        if not payload["messages"]:
            raise BusinessError("SESSION_NOT_FOUND", "会话不存在或已被删除")
        return payload

    @router.post("/api/{user_id}/sessions/{session_id}/activate")
    async def activate_session(
        user_id: str,
        session_id: str,
        current_user: User = Depends(require_path_user),
    ):
        instance = manager.get(user_id)
        if not instance or not instance.initialized:
            raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")
        try:
            return instance.activate_chat_session(session_id)
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
        instance = manager.get(user_id)
        if not instance or not instance.initialized:
            raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")
        try:
            instance.rename_chat_session(session_id, title)
        except ValueError:
            raise BusinessError("SESSION_NOT_FOUND", "会话不存在或已被删除")
        return {"session_id": session_id, "title": title[:80]}

    @router.delete("/api/{user_id}/sessions/{session_id}")
    async def delete_session(
        user_id: str,
        session_id: str,
        current_user: User = Depends(require_path_user),
    ):
        instance = manager.get(user_id)
        if not instance or not instance.initialized:
            raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")
        return {"active_session_id": instance.delete_chat_session(session_id)}

    @router.delete("/api/{user_id}/history")
    async def clear_chat_history(
        user_id: str,
        current_user: User = Depends(require_path_user),
    ):
        instance = manager.get(user_id)
        if not instance or not instance.initialized:
            raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")
        return {"active_session_id": instance.clear_chat_history()}

    return router
