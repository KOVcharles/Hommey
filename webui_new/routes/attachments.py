"""附件上传/查询/删除 API 路由（多模态 P0）。

路由层只做鉴权（require_path_user，handler 内再用 current_user.id 做附件归属二次
校验）与异常转换；校验、存盘、解析、状态机都在 AttachmentService 里。
"""
import logging

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.params import File
from fastapi.responses import Response

from settings import ATTACHMENT_CONFIG
from utils.io_executor import run_blocking
from utils.logging_safety import sanitize_for_log
from webui_new.auth import User, require_path_user
from webui_new.core.errors import AppError, BusinessError, InternalError, request_id

logger = logging.getLogger(__name__)


async def _read_limited(file: UploadFile) -> bytes:
    """Read at most the configured upload limit into request memory."""
    max_bytes = int(ATTACHMENT_CONFIG["max_size_bytes"])
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(min(1024 * 1024, max_bytes + 1)):
        total += len(chunk)
        if total > max_bytes:
            raise BusinessError(
                "ATTACHMENT_TOO_LARGE",
                f"单个文件超过 {max_bytes // (1024 * 1024)} MB 限制",
                status_code=400,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def create_attachments_router(attachment_service):
    """创建附件 router；attachment_service 由 server.py 注入共享单例。"""
    router = APIRouter()

    @router.post("/api/{user_id}/attachments")
    async def upload_attachment(
        request: Request,
        user_id: str,
        file: UploadFile = File(...),
        current_user: User = Depends(require_path_user),
    ):
        content = await _read_limited(file)
        filename = file.filename or "upload"
        try:
            result = await run_blocking(
                attachment_service.upload,
                user_id=str(current_user.id),
                filename=filename,
                content=content,
                request_id=request_id(request) or None,
            )
            return result.model_dump()
        except AppError:
            raise
        except Exception as e:
            logger.error(
                "附件上传失败 user_id=%s size=%d: %s",
                user_id, len(content), sanitize_for_log(e),
            )
            raise InternalError("ATTACHMENT_UPLOAD_FAILED", "附件上传失败，请稍后重试")

    @router.get("/api/{user_id}/attachments")
    async def list_attachments(
        user_id: str,
        limit: int = 100,
        current_user: User = Depends(require_path_user),
    ):
        """附件面板：该用户全部上传附件，按创建时间倒序。"""
        return {
            "attachments": [
                att.model_dump() for att in attachment_service.list(str(current_user.id), limit=limit)
            ]
        }

    @router.get("/api/{user_id}/attachments/{attachment_id}")
    async def get_attachment(
        user_id: str,
        attachment_id: str,
        current_user: User = Depends(require_path_user),
    ):
        # require_path_user 已确认 user_id 与 token 一致；service 再按 id+user_id 查询。
        return attachment_service.get(attachment_id, str(current_user.id)).model_dump()

    @router.get("/api/{user_id}/attachments/{attachment_id}/content")
    async def get_attachment_content(
        user_id: str,
        attachment_id: str,
        current_user: User = Depends(require_path_user),
    ):
        """下载附件原文件（私有存储，仅拥有者可访问）。"""
        filename, content = attachment_service.get_content(
            attachment_id, str(current_user.id)
        )
        encoded = quote(filename or "attachment")
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.delete("/api/{user_id}/attachments/{attachment_id}")
    async def delete_attachment(
        user_id: str,
        attachment_id: str,
        current_user: User = Depends(require_path_user),
    ):
        try:
            attachment_service.delete(attachment_id, str(current_user.id))
        except AppError:
            raise
        except Exception as e:
            logger.error("附件删除失败 user_id=%s id=%s: %s", user_id, attachment_id, sanitize_for_log(e))
            raise InternalError("ATTACHMENT_DELETE_FAILED", "附件删除失败，请稍后重试")
        return {"id": attachment_id, "deleted": True}

    return router
