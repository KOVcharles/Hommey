"""AttachmentService: upload pipeline + input normalization (P0).

- upload：校验 → 存盘 → INSERT → 同步解析 → 写 extraction → ready/failed
- normalize：加载附件 + 归属/状态校验 → 产出 NormalizedInput(agent_query, display_message)

所有方法 user_id 来自 JWT（require_path_user），不信任请求体里的 user_id。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from settings import ATTACHMENT_CONFIG, MEMORY_CONFIG, VISION_CONFIG
from webui_new.core.errors import BusinessError

from . import context_builder, validation
from .processors import ProcessorRegistry
from .quota import DailyQuota, redis_config_from_settings
from .repository import AttachmentRepository
from .schemas import (
    ATTACHMENT_STATUS_FAILED,
    ATTACHMENT_STATUS_EXPIRED,
    ATTACHMENT_STATUS_PROCESSING,
    ATTACHMENT_STATUS_READY,
    Attachment,
    AttachmentDetailResponse,
    AttachmentUploadResponse,
    Extraction,
    NormalizedInput,
)
from .storage import LocalAttachmentStore

logger = logging.getLogger(__name__)


_vision_quota: DailyQuota | None = None


def get_vision_quota() -> DailyQuota:
    global _vision_quota
    if _vision_quota is None:
        _vision_quota = DailyQuota(
            "vision", int(VISION_CONFIG.get("daily_limit", 0) or 0), redis_config_from_settings()
        )
    return _vision_quota


def _parse_error_code(exc: Exception, kind: str) -> str:
    """解析失败时选择对外的错误码；图片走视觉专用错误码。"""
    if kind == "image":
        message = str(exc)
        if message in ("VISION_FAILED", "VISION_TIMEOUT", "VISION_NOT_CONFIGURED", "VISION_DISABLED"):
            return message
    return "PARSE_FAILED"


def _is_expired(attachment: Attachment) -> bool:
    if not attachment.expires_at:
        return False
    try:
        expires_at = datetime.fromisoformat(attachment.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


class AttachmentService:
    def __init__(
        self,
        store: LocalAttachmentStore | None = None,
        repository: AttachmentRepository | None = None,
        processors: ProcessorRegistry | None = None,
    ):
        self.store = store or LocalAttachmentStore()
        self.repository = repository or AttachmentRepository()
        self.processors = processors or ProcessorRegistry()

    # ---- 上传管道 ----------------------------------------------------------

    def upload(
        self,
        *,
        user_id: str,
        filename: str,
        content: bytes,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> AttachmentUploadResponse:
        validation.validate_size(len(content))
        ext, kind, mime = validation.detect_kind(filename, content)
        if ext not in self.processors.supported_extensions():
            raise BusinessError("UNSUPPORTED_FILE_TYPE", f"暂不支持 .{ext} 类型", status_code=400)
        if kind == "image":
            if not VISION_CONFIG.get("enabled"):
                raise BusinessError("VISION_DISABLED", "图片上传功能未开启（未配置视觉识别服务）", status_code=400)
            if not get_vision_quota().consume(user_id):
                raise BusinessError(
                    "VISION_QUOTA_EXCEEDED",
                    "今日图片识别次数已达上限，请明天再试",
                    status_code=429,
                )

        attachment_id = f"att_{uuid.uuid4().hex}"
        sha256 = self.store.sha256(content)
        object_key = self.store.save(user_id, attachment_id, content)

        attachment = Attachment(
            id=attachment_id,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            filename=filename,
            mime_type=mime,
            kind=kind,
            size_bytes=len(content),
            sha256=sha256,
            object_key=object_key,
            status=ATTACHMENT_STATUS_PROCESSING,
            expires_at=(
                datetime.now(timezone.utc)
                + timedelta(days=max(int(ATTACHMENT_CONFIG["retention_days"]), 1))
            ).isoformat(),
        )
        try:
            self.repository.create(attachment)
        except Exception:
            # Storage and metadata must not diverge if the database write fails.
            self.store.delete(object_key)
            raise

        try:
            result = self.processors.parse(ext, content, filename)
        except Exception as exc:
            logger.warning("附件解析失败 id=%s kind=%s size=%d: %s", attachment_id, kind, len(content), exc)
            error_code = _parse_error_code(exc, kind)
            self.repository.update_status(
                attachment_id, user_id, ATTACHMENT_STATUS_FAILED, error_code=error_code
            )
            return AttachmentUploadResponse(
                id=attachment_id,
                filename=filename,
                kind=kind,
                status=ATTACHMENT_STATUS_FAILED,
                mime_type=mime,
                size_bytes=len(content),
                error_code=error_code,
            )

        extraction = Extraction(
            attachment_id=attachment_id,
            parser_version=self.processors.parser_version_for(ext),
            content_text=result.content_text,
            structured=result.structured,
            char_count=result.char_count,
        )
        try:
            # Extraction and ready status become visible together.
            self.repository.complete_processing(extraction, user_id)
        except Exception:
            try:
                self.repository.update_status(
                    attachment_id,
                    user_id,
                    ATTACHMENT_STATUS_FAILED,
                    error_code="PERSIST_FAILED",
                )
            except Exception:
                logger.exception("附件失败状态写入失败 id=%s", attachment_id)
            raise
        return AttachmentUploadResponse(
            id=attachment_id,
            filename=filename,
            kind=kind,
            status=ATTACHMENT_STATUS_READY,
            mime_type=mime,
            size_bytes=len(content),
        )

    # ---- 查询 / 删除 -------------------------------------------------------

    def get(self, attachment_id: str, user_id: str) -> AttachmentDetailResponse:
        att = self.repository.get(attachment_id, user_id)
        if att is None:
            raise BusinessError("ATTACHMENT_NOT_FOUND", "附件不存在或无权访问", status_code=404)
        status = ATTACHMENT_STATUS_EXPIRED if _is_expired(att) else att.status
        return AttachmentDetailResponse(
            id=att.id,
            filename=att.filename,
            kind=att.kind,
            status=status,
            mime_type=att.mime_type,
            size_bytes=att.size_bytes,
            error_code=att.error_code,
            created_at=att.created_at,
        )

    def list(self, user_id: str, limit: int = 100) -> list[AttachmentDetailResponse]:
        """该用户全部附件（附件面板），按创建时间倒序。"""
        attachments = self.repository.list_by_user(user_id, limit=limit)
        details = []
        for att in attachments:
            status = ATTACHMENT_STATUS_EXPIRED if _is_expired(att) else att.status
            details.append(
                AttachmentDetailResponse(
                    id=att.id,
                    filename=att.filename,
                    kind=att.kind,
                    status=status,
                    mime_type=att.mime_type,
                    size_bytes=att.size_bytes,
                    error_code=att.error_code,
                    created_at=att.created_at,
                )
            )
        return details

    def get_content(self, attachment_id: str, user_id: str) -> tuple[str, bytes]:
        """返回 (原文件名, 原文件字节) 供下载；过期/缺失时报错。"""
        att = self.repository.get(attachment_id, user_id)
        if att is None:
            raise BusinessError("ATTACHMENT_NOT_FOUND", "附件不存在或无权访问", status_code=404)
        if _is_expired(att):
            raise BusinessError("ATTACHMENT_EXPIRED", "附件已过期", status_code=400)
        try:
            content = self.store.load(att.object_key)
        except Exception:
            raise BusinessError("ATTACHMENT_CONTENT_MISSING", "附件原文件缺失", status_code=404)
        return att.filename, content

    def delete(self, attachment_id: str, user_id: str) -> None:
        object_key = self.repository.delete(attachment_id, user_id)
        if object_key is None:
            raise BusinessError("ATTACHMENT_NOT_FOUND", "附件不存在或无权访问", status_code=404)
        try:
            self.store.delete(object_key)
        except Exception:
            logger.warning("附件原文件清理失败 object_key=%s", object_key)

    # ---- 输入规范化 --------------------------------------------------------

    def normalize(
        self,
        user_text: str,
        attachment_ids: list[str] | None,
        user_id: str,
    ) -> NormalizedInput:
        """方案 §1.1 / §4.5：产出 agent_query（喂 Agent）与 display_message（写记忆）。"""
        user_text = (user_text or "").strip()
        attachment_ids = list(dict.fromkeys(attachment_ids or []))
        if not attachment_ids:
            return NormalizedInput(agent_query=user_text, display_message=user_text)

        validation.validate_count(len(attachment_ids))
        attachments = self.repository.get_many(attachment_ids, user_id)
        found_ids = {a.id for a in attachments}
        if len(found_ids) != len(set(attachment_ids)):
            raise BusinessError("ATTACHMENT_NOT_FOUND", "部分附件不存在或无权访问", status_code=404)
        if any(_is_expired(attachment) for attachment in attachments):
            raise BusinessError(
                "ATTACHMENT_EXPIRED",
                "部分附件已过期，请重新上传",
                status_code=400,
            )
        not_ready = [a for a in attachments if a.status != ATTACHMENT_STATUS_READY]
        if not_ready:
            raise BusinessError(
                "ATTACHMENT_NOT_READY",
                "部分附件尚未处理完成或处理失败，请稍后重试或移除该附件",
                status_code=400,
            )

        extractions = []
        for att in attachments:
            extraction = self.repository.get_extraction(att.id)
            if extraction is None:
                extraction = Extraction(attachment_id=att.id, content_text="")
            extractions.append((att, extraction))

        agent_query, sources, warnings = context_builder.build_agent_query(user_text, extractions)
        display_message = context_builder.build_display_message(user_text, attachments)
        return NormalizedInput(
            agent_query=agent_query,
            display_message=display_message,
            sources=sources,
            warnings=warnings,
            attachments=attachments,
            attachment_ids=attachment_ids,
        )
