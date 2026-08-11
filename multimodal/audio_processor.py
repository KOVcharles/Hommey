"""AudioTranscriber: 语音 → 文本（Mode A，P1-B）。

语音输入走"转写为文本、以纯文本发送"的半双工流程：前端 MediaRecorder 录音 →
客户端转 16kHz mono WAV → POST /asr/transcribe → 可编辑文本回填输入框。
转写文本等同于用户手打文本（同信任级），不落附件表。

``AudioTranscriber`` 是抽象边界：当前默认 SiliconFlow 的 OpenAI 兼容
``/audio/transcriptions``；未来换供应商/本地 faster-whisper 只改工厂，
不碰路由与前端。
"""
from __future__ import annotations

import io
import logging
from typing import Optional

import httpx

from core.execution_budget import consume_external_call
from settings import ASR_CONFIG

logger = logging.getLogger(__name__)

_RETRIABLE_STATUS = {429, 500, 502, 503, 504}


class AudioTranscriptionError(RuntimeError):
    """ASR 转写失败（不可重试或重试耗尽）。args[0] 为对外错误码。"""


class AudioTranscriber:
    def __init__(self, *, api_key: str, base_url: str, model: str, timeout_sec: float):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_sec = timeout_sec

    async def transcribe(
        self,
        audio_bytes: bytes,
        filename: str,
        language: Optional[str] = None,
    ) -> str:
        raise NotImplementedError


class SiliconFlowTranscriber(AudioTranscriber):
    """OpenAI 兼容 ``/audio/transcriptions``（multipart: file + model）。"""

    async def transcribe(
        self,
        audio_bytes: bytes,
        filename: str,
        language: Optional[str] = None,
    ) -> str:
        endpoint = f"{self.base_url}/audio/transcriptions"
        files = {"file": (filename or "audio.wav", audio_bytes, "audio/wav")}
        data = {"model": self.model, "response_format": "json"}
        if language:
            data["language"] = language
        headers = {"Authorization": f"Bearer {self.api_key}"}

        last_error: str | None = None
        for attempt in range(3):
            consume_external_call("asr")
            try:
                async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                    resp = await client.post(endpoint, files=files, data=data, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"asr transport error: {exc}"
                if attempt < 2:
                    continue
                raise AudioTranscriptionError("ASR_TIMEOUT", "语音转写超时，请稍后重试") from exc

            if resp.status_code in _RETRIABLE_STATUS and attempt < 2:
                last_error = f"asr http {resp.status_code}"
                continue
            if resp.status_code != 200:
                raise AudioTranscriptionError("ASR_FAILED", f"语音转写服务返回 {resp.status_code}")

            try:
                payload = resp.json()
                text = str(payload.get("text") or "").strip()
            except (ValueError, TypeError):
                raise AudioTranscriptionError("ASR_FAILED", "语音转写响应格式异常")
            if not text:
                raise AudioTranscriptionError("ASR_EMPTY_RESULT", "未能识别到语音内容")
            return text

        raise AudioTranscriptionError("ASR_FAILED", last_error or "语音转写失败")


def create_transcriber(config: Optional[dict] = None) -> AudioTranscriber | None:
    """按配置构建转写器；未启用或缺少 key 时返回 None（路由给出明确错误）。"""
    cfg = config or ASR_CONFIG
    if not cfg.get("enabled"):
        return None
    api_key = cfg.get("api_key") or ""
    if not api_key:
        return None
    return SiliconFlowTranscriber(
        api_key=api_key,
        base_url=str(cfg.get("base_url", "")),
        model=str(cfg.get("model", "")),
        timeout_sec=float(cfg.get("timeout_sec", 60.0)),
    )
