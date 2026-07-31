"""Local filesystem attachment store (P0).

附件原文件按 {storage_path}/{user_id}/{attachment_id} 隔离存储。user_id 来自 JWT
（可信整型）、attachment_id 为服务端生成的 uuid——两者均不含路径分隔符，且文件名
**不**进入存储路径，故无路径穿越风险。生产环境由 docker 卷挂载持久化。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from settings import ATTACHMENT_CONFIG


class LocalAttachmentStore:
    def __init__(self, storage_path: str | None = None):
        self.root = Path(storage_path or ATTACHMENT_CONFIG["storage_path"])
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_segment(value: str) -> str:
        # 防御性校验：段内不得包含路径分隔符（user_id/attachment_id 均应满足）。
        if not value or "/" in value or "\\" in value or value in (".", ".."):
            raise ValueError(f"unsafe storage segment: {value!r}")
        return value

    def object_key(self, user_id: str, attachment_id: str) -> str:
        return f"{self._safe_segment(user_id)}/{self._safe_segment(attachment_id)}"

    def save(self, user_id: str, attachment_id: str, data: bytes) -> str:
        key = self.object_key(user_id, attachment_id)
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def load(self, object_key: str) -> bytes:
        return (self.root / object_key).read_bytes()

    def delete(self, object_key: str) -> None:
        path = self.root / object_key
        if path.exists():
            path.unlink()

    @staticmethod
    def sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()
