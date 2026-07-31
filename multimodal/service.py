"""AttachmentService: upload pipeline + input normalization (P0).

- upload：校验 → 存盘 → INSERT → 同步解析 → 写 extraction → ready/failed
- normalize：加载附件 + 归属/状态校验 → 产出 NormalizedInput(agent_query, display_message)
- bind：把附件关联到 chat_history 消息 id

所有方法 user_id 来自 JWT（require_path_user），不信任请求体里的 user_id。
"""
from __future__ import annotations

import logging
import uuid

from webui_new.core.errors import BusinessError

from . import context_builder, validation
from .processors import ProcessorRegistry
from .repository import AttachmentRepository
from .schemas import (
    ATTACHMENT_STATUS_FAILED,
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
        )
        self.repository.create(attachment)

        try:
            result = self.processors.parse(ext, content, filename)
        except Exception:
            logger.warning("附件解析失败 id=%s kind=%s size=%d", attachment_id, kind, len(content))
            self.repository.update_status(
                attachment_id, user_id, ATTACHMENT_STATUS_FAILED, error_code="PARSE_FAILED"
            )
            return AttachmentUploadResponse(
                id=attachment_id,
                filename=filename,
                kind=kind,
                status=ATTACHMENT_STATUS_FAILED,
                mime_type=mime,
                size_bytes=len(content),
                error_code="PARSE_FAILED",
            )

        extraction = Extraction(
            attachment_id=attachment_id,
            parser_version="document-p0-1",
            content_text=result.content_text,
            structured=result.structured,
            char_count=result.char_count,
        )
        self.repository.set_extraction(extraction)
        self.repository.update_status(attachment_id, user_id, ATTACHMENT_STATUS_READY)
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
        return AttachmentDetailResponse(
            id=att.id,
            filename=att.filename,
            kind=att.kind,
            status=att.status,
            mime_type=att.mime_type,
            size_bytes=att.size_bytes,
            error_code=att.error_code,
            created_at=att.created_at,
        )

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
        attachment_ids = list(attachment_ids or [])
        if not attachment_ids:
            return NormalizedInput(agent_query=user_text, display_message=user_text)

        validation.validate_count(len(attachment_ids))
        attachments = self.repository.get_many(attachment_ids, user_id)
        found_ids = {a.id for a in attachments}
        if len(found_ids) != len(set(attachment_ids)):
            raise BusinessError("ATTACHMENT_NOT_FOUND", "部分附件不存在或无权访问", status_code=404)
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

    def bind(self, message_id: int, attachment_ids: list[str] | None, user_id: str) -> None:
        """把附件关联到已写入的用户消息 id（chat_message_attachments）。"""
        if not attachment_ids:
            return
        self.repository.bind(message_id, list(attachment_ids), user_id)
