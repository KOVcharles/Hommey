from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest


TEST_DSN = os.getenv("HOMMEY_TEST_POSTGRES_DSN", "")


@pytest.mark.skipif(not TEST_DSN, reason="HOMMEY_TEST_POSTGRES_DSN is required")
def test_refresh_jobs_are_shared_claimed_once_and_recover_expired_lease(tmp_path):
    from context.postgres_pool import get_postgres_pool
    from rag.refresh_jobs import ActiveRefreshJobError, PostgresRAGRefreshJobRepository
    from webui_new.auth.migrations import apply_all_migrations
    from webui_new.knowledge_base_service import KnowledgeBaseManagementService

    apply_all_migrations(TEST_DSN)
    collection = f"refresh-test-{uuid.uuid4().hex}"
    pool = get_postgres_pool(TEST_DSN)
    repo_a = PostgresRAGRefreshJobRepository(
        TEST_DSN, collection, lease_seconds=15, retry_delay_seconds=1, pool=pool
    )
    repo_b = PostgresRAGRefreshJobRepository(
        TEST_DSN, collection, lease_seconds=15, retry_delay_seconds=1, pool=pool
    )
    documents = tmp_path / "documents"
    knowledge = tmp_path / "knowledge"
    documents.mkdir()
    service_a = KnowledgeBaseManagementService(documents, knowledge, job_repository=repo_a)
    service_b = KnowledgeBaseManagementService(documents, knowledge, job_repository=repo_b)

    uploaded = service_a.upload("policy.txt", "差旅住宿标准".encode())
    assert uploaded["source_generation"] == 1
    queued = service_a.start_refresh("7")
    assert queued["status"] == "queued"
    assert queued["source_generation"] == 1
    assert service_b.status()["job_id"] == queued["job_id"]
    with pytest.raises(ActiveRefreshJobError):
        repo_b.enqueue("8", service_b.source_snapshot)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                lambda item: item[0].claim(item[1]),
                [(repo_a, "worker-a"), (repo_b, "worker-b")],
            )
        )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    claimed = winners[0]
    owner = str(claimed["lease_owner"])
    assert claimed["attempt"] == 1
    assert repo_a.update_progress(queued["job_id"], owner, "embedding", 72)

    # Simulate a dead worker. A second process requeues and claims only after
    # the first owner's lease has expired; the stale owner can no longer commit.
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE rag_refresh_jobs SET lease_expires_at=NOW() - INTERVAL '1 second' WHERE job_id=%s",
            (queued["job_id"],),
        )
    reclaimed = repo_b.claim("worker-recovery")
    assert reclaimed is not None
    assert reclaimed["attempt"] == 2
    assert repo_a.renew(queued["job_id"], owner) is False

    report = {"status": "success", "documents_loaded": 1, "chunks_loaded": 1, "errors": []}
    manifest = {
        "refreshed_at": "2026-08-17T00:00:00+00:00",
        "documents": {"policy.txt": {"sha256": service_b.source_snapshot()[0]["sha256"]}},
        "report": report,
    }
    assert repo_b.complete(queued["job_id"], "worker-recovery", report, manifest)
    assert service_a.status()["status"] == "success"
    assert service_b.document_index_status("policy.txt", documents / "policy.txt") == "indexed"

    # The independent worker path claims, holds the collection lock, reports
    # completion and publishes a heartbeat without any web-process thread.
    from rag.config import RAGPipelineConfig
    from rag.refresh_worker import RAGRefreshWorker

    second = repo_a.enqueue("7", service_a.source_snapshot)

    class FakeWorker(RAGRefreshWorker):
        def _run_snapshot(self, job, progress):
            progress("fake build", 80)
            fake_report = {
                "status": "success",
                "documents_loaded": 1,
                "chunks_loaded": 1,
                "errors": [],
            }
            return fake_report, {"documents": manifest["documents"], "report": fake_report}

    worker = FakeWorker(
        repo_b,
        RAGPipelineConfig(postgres_dsn=TEST_DSN, collection_name=collection),
        worker_id="worker-process",
    )
    assert worker.run_once() is True
    assert repo_a.latest_status()["job_id"] == second["job_id"]
    assert repo_a.latest_status()["status"] == "success"

    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM rag_worker_heartbeats WHERE collection_name=%s", (collection,))
        cursor.execute("DELETE FROM rag_refresh_jobs WHERE collection_name=%s", (collection,))
        cursor.execute("DELETE FROM rag_source_generations WHERE collection_name=%s", (collection,))


def test_stage3_migration_contains_queue_lease_and_single_active_constraints():
    from webui_new.auth.migrations import MIGRATIONS_DIR

    sql = (MIGRATIONS_DIR / "0021_rag_refresh_control_plane.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS rag_refresh_jobs" in sql
    assert "lease_owner" in sql
    assert "lease_expires_at" in sql
    assert "FOR UPDATE SKIP LOCKED" not in sql  # claim semantics live in repository code
    assert "uq_rag_refresh_jobs_one_active" in sql
