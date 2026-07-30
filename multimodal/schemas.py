"""Pydantic schemas for the multimodal attachment subsystem (P0)."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

# 附件状态机：uploaded -> queued -> processing -> ready | failed | rejected
# P0 同步解析：上传即 processing，解析完成置 ready/failed。
ATTACHMENT_STATUS_UPLOADED = "uploaded"
ATTACHMENT_STATUS_PROCESSING = "processing"
ATTACHMENT_STATUS_READY = "ready"
ATTACHMENT_STATUS_FAILED = "failed"
ATTACHMENT_STATUS_REJECTED = "rejected"


class Attachment(BaseModel):
    """attachments 表行映射。id 形如 att_<uuid4 hex>。"""
    id: str
    user_id: str
    session_id: Optional[str] = None
    request_id: Optional[str] = None
    filename: str
    mime_type: Optional[str] = None
    kind: str = "document"
    size_bytes: int
    sha256: Optional[str] = None
    object_key: str
    status: str = ATTACHMENT_STATUS_UPLOADED
    error_code: Optional[str] = None
    created_at: Optional[str] = None
    expires_at: Optional[str] = None


class Extraction(BaseModel):
    """attachment_extractions 表行映射。"""
    attachment_id: str
    parser_version: Optional[str] = None
    language: Optional[str] = None
    content_text: str = ""
    structured: dict[str, Any] = Field(default_factory=dict)
    char_count: int = 0


class AttachmentSource(BaseModel):
    """NormalizedInput.sources 的元素：前端展示来源 + 多轮页码引用。"""
    attachment_id: str
    filename: str
    kind: str = "document"
    char_count: int = 0
    page_count: Optional[int] = None


class MessageInput(BaseModel):
    """路由层组装的原始输入（MessageInput(原始)，见方案 §5.1）。"""
    text: str = ""
    attachment_ids: list[str] = Field(default_factory=list)
    user_id: str
    session_id: Optional[str] = None
    request_id: Optional[str] = None


class NormalizedInput(BaseModel):
    """InputProcessingService.normalize 的产物（见方案 §1.1 / §4.5）。

    - agent_query：用户文本 + 经预算裁剪的附件上下文 → 喂意图/编排/文本模型，仅当次请求。
    - display_message：用户字面原文 + 紧凑附件清单 → 写 chat_history.content / 短期记忆 / 前端历史。
      绝不把 agent_query 或附件全文存进记忆。
    """
    agent_query: str
    display_message: str
    sources: list[AttachmentSource] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    attachment_ids: list[str] = Field(default_factory=list)


class AttachmentUploadResponse(BaseModel):
    id: str
    filename: str
    kind: str
    status: str
    mime_type: Optional[str] = None
    size_bytes: int = 0
    error_code: Optional[str] = None


class AttachmentDetailResponse(BaseModel):
    id: str
    filename: str
    kind: str
    status: str
    mime_type: Optional[str] = None
    size_bytes: int = 0
    error_code: Optional[str] = None
    created_at: Optional[str] = None
