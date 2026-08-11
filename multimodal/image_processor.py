"""ImageProcessor: 图片输入 → 视觉模型 → 文本 query（P1-A）。

图片在上传时同步处理：Pillow 解码校验 → 降采样（限制请求体与成本）→
base64 data URL → VisionClient（SiliconFlow Qwen2.5-VL，OpenAI 兼容）→
产出 描述 + OCR + 差旅结构化字段。提取结果进 attachment_extractions，
下游 normalize/context_builder 零改动即可把图片内容拼入 agent_query。

依赖惰性导入：未安装 Pillow 或未开启 VISION_CONFIG.enabled 时给出清晰错误。
"""
from __future__ import annotations

import io
import logging

from settings import VISION_CONFIG

from .document_processor import DocumentProcessor, ParseResult
from .vision_client import VisionClient, VisionError

logger = logging.getLogger(__name__)

IMAGE_PARSER_VERSION = "image-p0-1"


class ImageProcessor(DocumentProcessor):
    supported_extensions = ("png", "jpg", "jpeg", "webp")
    parser_version = IMAGE_PARSER_VERSION

    def parse(self, data: bytes, filename: str) -> ParseResult:
        if not VISION_CONFIG.get("enabled"):
            raise RuntimeError("图片理解未启用（HOMMEY_VISION_ENABLED=false）")
        if not VISION_CONFIG.get("api_key"):
            raise RuntimeError("未配置视觉 API key（HOMMEY_VISION_API_KEY / SILICONFLOW_API_KEY）")

        try:
            from PIL import Image  # type: ignore
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise RuntimeError("图片解析需要 Pillow 依赖") from exc

        try:
            with Image.open(io.BytesIO(data)) as probe:
                probe.verify()  # 触发完整解码以捕获损坏/伪装的图片。
            image = Image.open(io.BytesIO(data))
            image.load()
        except Exception as exc:
            raise RuntimeError("图片解码失败或文件已损坏") from exc

        image = _downsample(image, max_pixels=int(VISION_CONFIG.get("max_pixels", 0)) or None)

        # 统一转 RGB + PNG 输出，避免格式相关的解码分支（webp→png 保持无损）。
        rgb = image.convert("RGB")
        buffer = io.BytesIO()
        rgb.save(buffer, format="PNG")
        normalized = buffer.getvalue()

        try:
            result = VisionClient().describe_image(normalized, "png", filename)
        except VisionError as exc:
            code = exc.args[0] if exc.args else "VISION_FAILED"
            logger.warning("图片理解失败 filename=%s code=%s", filename, code)
            raise RuntimeError(code) from exc

        return ParseResult(
            content_text=result.content_text,
            structured=result.structured,
            char_count=result.char_count,
        )


def _downsample(image, max_pixels: int | None):
    """按总像素上限等比降采样，控制发送给视觉模型的请求体大小。"""
    if not max_pixels or max_pixels <= 0:
        return image
    width, height = image.size
    if width * height <= max_pixels:
        return image
    ratio = (max_pixels / (width * height)) ** 0.5
    new_width = max(1, int(width * ratio))
    new_height = max(1, int(height * ratio))
    from PIL import Image

    return image.resize((new_width, new_height), Image.Resampling.LANCZOS)
