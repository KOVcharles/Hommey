# -*- coding: utf-8 -*-
"""在容器内入队一次 RAG 刷新并轮询直到完成（供 docker exec 调用）。"""
import sys
import time

sys.path.insert(0, "/app")

from rag.config import RAGPipelineConfig
from rag.refresh_jobs import PostgresRAGRefreshJobRepository
from webui_new.knowledge_base_service import KnowledgeBaseManagementService


def main() -> int:
    config = RAGPipelineConfig.from_settings()
    repo = PostgresRAGRefreshJobRepository(config.postgres_dsn, config.collection_name)
    svc = KnowledgeBaseManagementService(
        config.documents_dir,
        config.knowledge_base_path,
        config=config,
        job_repository=repo,
    )

    try:
        result = svc.start_refresh("cli")
        print("ENQUEUED", result.get("job_id"), result.get("status"))
    except Exception as exc:  # noqa: BLE001
        print("ENQUEUE_ERROR", type(exc).__name__, str(exc))
        # 已有排队/运行中的任务时，继续轮询当前状态即可。
        print("existing job detected, continuing to poll...")

    for i in range(120):
        status = repo.latest_status()
        state = status.get("status")
        print(
            f"poll {i}: status={state} progress={status.get('progress')} "
            f"stage={status.get('stage')} job={status.get('job_id')}"
        )
        if state in ("success", "partial_success", "error"):
            if status.get("report"):
                print("REPORT", str(status.get("report"))[:800])
            return 0 if state == "success" else 1
        time.sleep(3)

    print("TIMEOUT waiting for refresh")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
