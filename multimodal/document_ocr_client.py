"""Document-focused OCR client shared by Query PDF and RAG ingestion.

Unlike :mod:`multimodal.vision_client`, this client has one narrow contract:
transcribe a rendered document page to Markdown without describing the image or
extracting travel-specific fields.  Keeping the contracts separate lets the
image-understanding and document-OCR models evolve independently.
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from core.execution_budget import consume_external_call
from settings import OCR_CONFIG

logger = logging.getLogger(__name__)

_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}

# DeepSeek-OCR requires the model's native image marker in the text prompt.
# The extracted text is still wrapped as untrusted attachment data downstream.
_USER_PROMPT = "<image>\n<|grounding|>Convert the document to markdown."

_RETRIABLE_STATUS = {429, 500, 502, 503, 504}


class DocumentOcrError(RuntimeError):
    """Stable document-OCR failure exposed to the attachment pipeline."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.public_message = message
        super().__init__(message)


@dataclass(frozen=True)
class DocumentOcrResult:
    text: str
    model: str
    confidence: float | None = None


class DocumentOcrClient:
    """Call an OpenAI-compatible vision endpoint for page-level OCR."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_sec: float | None = None,
        max_retries: int | None = None,
        max_tokens: int | None = None,
    ):
        self.enabled = bool(OCR_CONFIG.get("enabled")) if enabled is None else bool(enabled)
        self.api_key = (OCR_CONFIG.get("api_key") or "") if api_key is None else api_key
        configured_base_url = OCR_CONFIG.get("base_url") if base_url is None else base_url
        configured_model = OCR_CONFIG.get("model") if model is None else model
        self.base_url = str(configured_base_url or "").rstrip("/")
        self.model = str(configured_model or "")
        self.timeout_sec = (
            float(OCR_CONFIG.get("timeout_sec", 30.0))
            if timeout_sec is None
            else float(timeout_sec)
        )
        configured_retries = OCR_CONFIG.get("max_retries", 2) if max_retries is None else max_retries
        self.max_retries = max(int(configured_retries), 0)
        configured_tokens = OCR_CONFIG.get("max_tokens", 4096) if max_tokens is None else max_tokens
        self.max_tokens = max(int(configured_tokens), 1)

    def ocr_page(
        self,
        data: bytes,
        ext: str = "png",
        *,
        filename: str = "",
        page_number: int | None = None,
    ) -> DocumentOcrResult:
        """Transcribe one rendered document page to Markdown."""
        self._validate_configuration()
        if not data:
            raise DocumentOcrError("OCR_FAILED", "待识别页面为空")

        mime = _MIME_BY_EXT.get((ext or "").lower(), "image/png")
        data_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": _USER_PROMPT},
                    ],
                },
            ],
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(self.max_retries + 1):
            consume_external_call("ocr")
            try:
                with httpx.Client(timeout=self.timeout_sec) as client:
                    response = client.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self.max_retries:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise DocumentOcrError("OCR_TIMEOUT", "PDF 页面 OCR 超时，请稍后重试") from exc

            if response.status_code in _RETRIABLE_STATUS and attempt < self.max_retries:
                time.sleep(0.5 * (2**attempt))
                continue
            if response.status_code != 200:
                logger.warning(
                    "Document OCR failed status=%d filename=%s page=%s",
                    response.status_code,
                    filename,
                    page_number,
                )
                raise DocumentOcrError("OCR_FAILED", "PDF 页面 OCR 服务调用失败")

            text = self._response_text(response.json())
            if not text:
                raise DocumentOcrError("OCR_EMPTY_RESULT", "PDF 页面未识别到文字")
            return DocumentOcrResult(text=text, model=self.model)

        raise DocumentOcrError("OCR_FAILED", "PDF 页面 OCR 失败")

    def _validate_configuration(self) -> None:
        if not self.enabled:
            raise DocumentOcrError("OCR_DISABLED", "PDF OCR 服务未开启")
        if not self.api_key:
            raise DocumentOcrError("OCR_NOT_CONFIGURED", "未配置 PDF OCR API Key")
        if not self.base_url or not self.model:
            raise DocumentOcrError("OCR_NOT_CONFIGURED", "PDF OCR 服务配置不完整")

    @staticmethod
    def _response_text(payload: dict[str, Any]) -> str:
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DocumentOcrError("OCR_FAILED", "PDF OCR 响应格式异常") from exc
        if not isinstance(content, str):
            raise DocumentOcrError("OCR_FAILED", "PDF OCR 响应格式异常")
        return _strip_outer_markdown_fence(content.strip())


def _strip_outer_markdown_fence(text: str) -> str:
    """Remove a single outer Markdown fence without changing page content."""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text
