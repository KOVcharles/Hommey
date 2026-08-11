"""语音转写 API（Mode A：转写为文本后以纯文本发送，不落附件表）。

路由只做鉴权、限长与异常转换；真正的转写逻辑在 AudioTranscriber（multimodal/）。
"""
import logging

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.params import File

from settings import ASR_CONFIG
from multimodal.audio_processor import AudioTranscriptionError, create_transcriber
from multimodal.quota import DailyQuota, redis_config_from_settings
from webui_new.auth import User, require_path_user
from webui_new.core.errors import AppError, BusinessError, InternalError, request_id

logger = logging.getLogger(__name__)

_asr_quota: DailyQuota | None = None


def _get_asr_quota() -> DailyQuota:
    global _asr_quota
    if _asr_quota is None:
        _asr_quota = DailyQuota(
            "asr", int(ASR_CONFIG.get("daily_limit", 0) or 0), redis_config_from_settings()
        )
    return _asr_quota


async def _read_limited(file: UploadFile) -> bytes:
    """Read at most the ASR upload limit into request memory."""
    max_bytes = int(ASR_CONFIG["max_size_bytes"])
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(min(1024 * 1024, max_bytes + 1)):
        total += len(chunk)
        if total > max_bytes:
            raise BusinessError(
                "ASR_FILE_TOO_LARGE",
                f"语音文件超过 {max_bytes // (1024 * 1024)} MB 限制",
                status_code=400,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def create_asr_router():
    """创建语音转写 router；转写器按配置懒构建。"""
    router = APIRouter()

    @router.post("/api/{user_id}/asr/transcribe")
    async def transcribe_audio(
        user_id: str,
        file: UploadFile = File(...),
        language: str | None = None,
        current_user: User = Depends(require_path_user),
    ):
        transcriber = create_transcriber()
        if transcriber is None:
            raise BusinessError(
                "ASR_DISABLED",
                "语音转写功能未开启（未配置 ASR 服务）",
                status_code=400,
            )
        if not _get_asr_quota().consume(str(current_user.id)):
            raise BusinessError(
                "ASR_QUOTA_EXCEEDED",
                "今日语音转写次数已达上限，请明天再试",
                status_code=429,
            )

        content = await _read_limited(file)
        filename = file.filename or "voice.wav"
        try:
            text = await transcriber.transcribe(content, filename, language=language)
        except AudioTranscriptionError as exc:
            code = exc.args[0] if exc.args else "ASR_FAILED"
            logger.warning("语音转写失败 user_id=%s size=%d code=%s", user_id, len(content), code)
            raise BusinessError(code, str(exc.args[1]) if len(exc.args) > 1 else "语音转写失败", status_code=400)
        except Exception as e:
            logger.error("语音转写异常 user_id=%s size=%d", user_id, len(content))
            raise InternalError("ASR_TRANSCRIBE_FAILED", "语音转写失败，请稍后重试") from e

        return {"text": text}

    return router
