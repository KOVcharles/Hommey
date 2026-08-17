"""Independent worker that executes durable PostgreSQL RAG refresh jobs."""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import shutil
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import RAGPipelineConfig
from .pipeline import RAGPipeline
from .refresh_jobs import PostgresRAGRefreshJobRepository


logger = logging.getLogger(__name__)


class RefreshLeaseLostError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RAGRefreshWorker:
    def __init__(
        self,
        repository: PostgresRAGRefreshJobRepository,
        config: RAGPipelineConfig,
        *,
        worker_id: str | None = None,
        poll_seconds: float = 2.0,
    ):
        self.repository = repository
        self.config = config
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.poll_seconds = max(0.2, float(poll_seconds))

    def run_once(self) -> bool:
        """Claim and execute at most one job; return whether one was claimed."""
        self.repository.heartbeat(self.worker_id, {"pid": os.getpid()})
        with self.repository.pool.connection() as lock_connection:
            if not self.repository.try_collection_lock(lock_connection):
                return False
            try:
                job = self.repository.claim(self.worker_id)
                if not job:
                    return False
                self._execute(job)
                return True
            finally:
                self.repository.release_collection_lock(lock_connection)

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        stop = stop_event or threading.Event()
        logger.info("RAG refresh worker started: worker_id=%s", self.worker_id)
        while not stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                logger.exception("RAG refresh worker loop failed")
                worked = False
            if not worked:
                stop.wait(self.poll_seconds)

    def _execute(self, job: dict[str, Any]) -> None:
        job_id = str(job["job_id"])
        lease_lost = threading.Event()
        heartbeat_stop = threading.Event()
        interval = max(2.0, self.repository.lease_seconds / 3)

        def heartbeat() -> None:
            while not heartbeat_stop.wait(interval):
                try:
                    self.repository.heartbeat(self.worker_id, {"pid": os.getpid(), "job_id": job_id})
                    if not self.repository.renew(job_id, self.worker_id):
                        lease_lost.set()
                        return
                except Exception:
                    logger.exception("RAG refresh lease renewal failed: job_id=%s", job_id)
                    lease_lost.set()
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"rag-refresh-heartbeat-{job_id[:8]}",
            daemon=True,
        )
        heartbeat_thread.start()

        def progress(stage: str, value: int) -> None:
            if lease_lost.is_set():
                raise RefreshLeaseLostError("RAG refresh lease was lost")
            if not self.repository.update_progress(job_id, self.worker_id, stage, value):
                lease_lost.set()
                raise RefreshLeaseLostError("RAG refresh job is no longer owned by this worker")

        try:
            report, result_manifest = self._run_snapshot(job, progress)
            if lease_lost.is_set() or not self.repository.renew(job_id, self.worker_id):
                raise RefreshLeaseLostError("RAG refresh lease was lost before completion")
            if str(report.get("status")) not in {"success", "partial_success"}:
                message = str(report.get("message") or report.get("errors") or "RAG refresh failed")
                self.repository.fail(job_id, self.worker_id, message, retryable=False)
                return
            if not self.repository.complete(job_id, self.worker_id, report, result_manifest):
                raise RefreshLeaseLostError("RAG refresh completion was rejected for a stale lease")
        except RefreshLeaseLostError:
            logger.error("RAG refresh stopped after lease loss: job_id=%s", job_id)
        except Exception as exc:
            logger.exception("RAG refresh failed: job_id=%s", job_id)
            self.repository.fail(job_id, self.worker_id, str(exc), retryable=True)
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            self.repository.heartbeat(self.worker_id, {"pid": os.getpid()})

    def _run_snapshot(self, job: dict[str, Any], progress) -> tuple[dict, dict]:
        source_root = Path(self.config.documents_dir).resolve()
        manifest = list(job.get("source_manifest") or [])
        with tempfile.TemporaryDirectory(prefix="hommey-rag-snapshot-") as temporary:
            snapshot_root = Path(temporary).resolve()
            for entry in manifest:
                document_id = str(entry.get("document_id") or "")
                source = (source_root / document_id).resolve()
                try:
                    source.relative_to(source_root)
                except ValueError as exc:
                    raise RuntimeError(f"source snapshot escapes root: {document_id}") from exc
                if not source.is_file():
                    raise RuntimeError(f"source snapshot file is missing: {document_id}")
                if _sha256(source) != str(entry.get("sha256") or ""):
                    raise RuntimeError(f"source snapshot hash changed: {document_id}")
                target = (snapshot_root / document_id).resolve()
                try:
                    target.relative_to(snapshot_root)
                except ValueError as exc:
                    raise RuntimeError(f"snapshot target escapes root: {document_id}") from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

            snapshot_config = dataclasses.replace(self.config, documents_dir=str(snapshot_root))
            pipeline = RAGPipeline(config=snapshot_config)
            try:
                report = pipeline.ingest(
                    snapshot_root,
                    rebuild=True,
                    progress_callback=progress,
                ).to_dict()
            finally:
                pipeline.close()

        # PostgreSQL is authoritative; this file remains a compatibility export
        # for local tooling and can be regenerated from the completed job.
        from webui_new.knowledge_base_service import KnowledgeBaseManagementService

        service = KnowledgeBaseManagementService(
            source_root,
            self.config.knowledge_base_path,
            config=self.config,
        )
        result_manifest = service._write_manifest(report, source_manifest=manifest)
        return report, result_manifest
