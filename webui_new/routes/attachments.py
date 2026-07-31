"""附件上传/查询/删除 API 路由（多模态 P0）。

路由层只做鉴权（require_path_user，handler 内再用 current_user.id 做附件归属二次
校验）与异常转换；校验、存盘、解析、状态机都在 AttachmentService 里。
"""
import logging

from fastapi import APIRouter, Depends, UploadFile
from fastapi.params import File

from utils.logging_safety import sanitize_for_log
from webui_new.auth import User, require_path_user
from webui_new.core.errors import AppError, InternalError

logger = logging.getLogger(__name__)


def create_attachments_router(attachment_service):
    """创建附件 router；attachment_service 由 server.py 注入共享单例。"""
    router = APIRouter()

    @router.post("/api/{user_id}/attachments")
    async def upload_attachment(
        user_id: str,
        file: UploadFile = File(...),
        current_user: User = Depends(require_path_user),
    ):
        content = await file.read()
        filename = file.filename or "upload"
        try:
            return attachment_service.upload(
                user_id=str(current_user.id),
                filename=filename,
                content=content,
            ).model_dump()
        except AppError:
            raise
        except Exception as e:
            logger.error(
                "附件上传失败 user_id=%s size=%d: %s",
                user_id, len(content), sanitize_for_log(e),
            )
            raise InternalError("ATTACHMENT_UPLOAD_FAILED", "附件上传失败，请稍后重试")

    @router.get("/api/{user_id}/attachments/{attachment_id}")
    async def get_attachment(
        user_id: str,
        attachment_id: str,
        current_user: User = Depends(require_path_user),
    ):
        # require_path_user 已确认 user_id 与 token 一致；service 再按 id+user_id 查询。
        return attachment_service.get(attachment_id, str(current_user.id)).model_dump()

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
