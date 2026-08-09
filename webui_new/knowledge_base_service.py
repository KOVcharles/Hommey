"""Knowledge-base source uploads and background RAG rebuild orchestration."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from rag.config import RAGPipelineConfig
from rag.pipeline import RAGPipeline
from webui_new.core.errors import BusinessError

logger = logging.getLogger(__name__)

SUPPORTED_UPLOAD_TYPES = {"txt", "md", "pdf"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_FILES = 10


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KnowledgeBaseManagementService:
    """Manage source files and serialize full knowledge-base rebuild jobs."""

    def __init__(
        self,
        documents_dir: str | Path,
        knowledge_base_path: str | Path,
        *,
        config: RAGPipelineConfig | None = None,
        ingestion_runner: Callable[[Callable[[str, int], None]], dict] | None = None,
    ):
        self.documents_dir = Path(documents_dir).resolve()
        self.knowledge_base_path = Path(knowledge_base_path).resolve()
        self.manifest_root = (
            self.knowledge_base_path.parent
            if self.knowledge_base_path.suffix.lower() == ".db"
            else self.knowledge_base_path
        )
        self.manifest_path = self.manifest_root / "ingestion_manifest.json"
        self.config = config or RAGPipelineConfig.from_settings(
            {
                "documents_dir": str(self.documents_dir),
                "knowledge_base_path": str(self.knowledge_base_path),
            }
        )
        self._ingestion_runner = ingestion_runner or self._run_pipeline
        self._lock = threading.Lock()
        self._source_lock = threading.Lock()
        self._job = self._initial_status()

    def upload(self, filename: str, content: bytes) -> dict:
        with self._source_lock:
            return self._upload_locked(filename, content)

    def _upload_locked(self, filename: str, content: bytes) -> dict:
        with self._lock:
            if self._job.get("status") == "running":
                raise BusinessError(
                    "KNOWLEDGE_REFRESH_RUNNING",
                    "知识库正在刷新，完成后再上传新文档",
                    status_code=409,
                )
        safe_name = self._safe_filename(filename)
        file_type = Path(safe_name).suffix.lower().lstrip(".")
        if file_type not in SUPPORTED_UPLOAD_TYPES:
            raise BusinessError(
                "KNOWLEDGE_FILE_TYPE_UNSUPPORTED",
                "仅支持 TXT、Markdown 和 PDF 文档",
                status_code=400,
            )
        if not content:
            raise BusinessError("KNOWLEDGE_FILE_EMPTY", "不能上传空文档", status_code=400)
        if len(content) > MAX_UPLOAD_BYTES:
            raise BusinessError(
                "KNOWLEDGE_FILE_TOO_LARGE",
                f"单个文档不能超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
                status_code=400,
            )
        self._validate_content(file_type, content)

        self.documents_dir.mkdir(parents=True, exist_ok=True)
        destination = (self.documents_dir / safe_name).resolve()
        try:
            destination.relative_to(self.documents_dir)
        except ValueError as exc:
            raise BusinessError("KNOWLEDGE_FILENAME_INVALID", "文档名称不合法", status_code=400) from exc

        existed = destination.exists()
        temporary = self.documents_dir / f".{uuid.uuid4().hex}.upload"
        try:
            with temporary.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

        return {
            "filename": safe_name,
            "file_type": file_type,
            "size_bytes": len(content),
            "status": "updated" if existed else "created",
            "index_status": "pending",
        }

    def document_index_status(self, document_id: str, path: Path) -> str:
        manifest = self._read_manifest()
        indexed = manifest.get("documents", {}).get(document_id)
        if not indexed:
            return "pending"
        try:
            return "indexed" if indexed.get("sha256") == self._sha256(path) else "pending"
        except OSError:
            return "pending"

    def start_refresh(self, requested_by: str) -> dict:
        with self._source_lock:
            with self._lock:
                if self._job.get("status") == "running":
                    raise BusinessError(
                        "KNOWLEDGE_REFRESH_RUNNING",
                        "知识库正在刷新，请等待当前任务完成",
                        status_code=409,
                    )
                self._job = {
                    "job_id": uuid.uuid4().hex[:12],
                    "status": "running",
                    "stage": "正在准备知识库刷新",
                    "progress": 5,
                    "requested_by": str(requested_by),
                    "started_at": _utc_now(),
                    "finished_at": None,
                    "report": None,
                    "message": None,
                }
                payload = dict(self._job)

        thread = threading.Thread(target=self._refresh_worker, name="hommey-rag-refresh", daemon=True)
        thread.start()
        return payload

    def status(self) -> dict:
        with self._lock:
            return dict(self._job)

    def _refresh_worker(self) -> None:
        try:
            report = self._ingestion_runner(self._update_progress)
            raw_status = str(report.get("status") or "error")
            final_status = raw_status if raw_status in {"success", "partial_success"} else "error"
            if final_status in {"success", "partial_success"}:
                self._write_manifest(report)
            with self._lock:
                self._job.update(
                    {
                        "status": final_status,
                        "stage": "知识库刷新完成" if final_status == "success" else "知识库刷新完成，但有部分文档失败",
                        "progress": 100,
                        "finished_at": _utc_now(),
                        "report": report,
                        "message": report.get("message"),
                    }
                )
        except Exception:
            logger.exception("Knowledge-base refresh failed")
            with self._lock:
                self._job.update(
                    {
                        "status": "error",
                        "stage": "知识库刷新失败",
                        "progress": 100,
                        "finished_at": _utc_now(),
                        "message": "刷新失败，请检查服务配置后重试",
                    }
                )

    def _run_pipeline(self, progress_callback: Callable[[str, int], None]) -> dict:
        pipeline = RAGPipeline(config=self.config)
        try:
            return pipeline.ingest(
                self.documents_dir,
                rebuild=True,
                progress_callback=progress_callback,
            ).to_dict()
        finally:
            pipeline.close()

    def _update_progress(self, stage: str, progress: int) -> None:
        with self._lock:
            if self._job.get("status") != "running":
                return
            self._job.update({"stage": stage, "progress": max(0, min(99, int(progress)))})

    def _write_manifest(self, report: dict) -> None:
        failed_sources = {
            Path(str(item.get("source_path") or "")).resolve()
            for item in report.get("errors", [])
            if item.get("source_path")
        }
        documents = {}
        if self.documents_dir.exists():
            for path in self.documents_dir.rglob("*"):
                if not path.is_file() or path.suffix.lower().lstrip(".") not in SUPPORTED_UPLOAD_TYPES:
                    continue
                resolved = path.resolve()
                if resolved in failed_sources:
                    continue
                try:
                    document_id = resolved.relative_to(self.documents_dir).as_posix()
                except ValueError:
                    continue
                documents[document_id] = {"sha256": self._sha256(resolved), "indexed_at": _utc_now()}
        payload = {"refreshed_at": _utc_now(), "documents": documents, "report": report}
        self.manifest_root.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.manifest_path)

    def _read_manifest(self) -> dict:
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}

    def _initial_status(self) -> dict:
        manifest = self._read_manifest()
        return {
            "job_id": None,
            "status": "idle",
            "stage": "知识库已就绪",
            "progress": 0,
            "requested_by": None,
            "started_at": None,
            "finished_at": manifest.get("refreshed_at"),
            "report": manifest.get("report"),
            "message": None,
        }

    @staticmethod
    def _safe_filename(filename: str) -> str:
        safe_name = str(filename or "").replace("\\", "/").split("/")[-1].strip()
        if not safe_name or safe_name in {".", ".."} or safe_name.startswith(".") or len(safe_name) > 180:
            raise BusinessError("KNOWLEDGE_FILENAME_INVALID", "文档名称不合法", status_code=400)
        return safe_name

    @staticmethod
    def _validate_content(file_type: str, content: bytes) -> None:
        if file_type in {"txt", "md"}:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BusinessError(
                    "KNOWLEDGE_TEXT_ENCODING_INVALID",
                    "文本文件必须使用 UTF-8 编码",
                    status_code=400,
                ) from exc
            if not text.strip():
                raise BusinessError("KNOWLEDGE_FILE_EMPTY", "不能上传空文档", status_code=400)
        elif not content.startswith(b"%PDF"):
            raise BusinessError("KNOWLEDGE_PDF_INVALID", "PDF 文件格式无效", status_code=400)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
