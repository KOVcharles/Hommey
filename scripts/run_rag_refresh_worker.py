#!/usr/bin/env python3
"""Run the persistent RAG refresh worker."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rag.config import RAGPipelineConfig
from rag.refresh_jobs import PostgresRAGRefreshJobRepository
from rag.refresh_worker import RAGRefreshWorker
from settings import RAG_CONFIG
from utils.structured_logging import configure_logging
from webui_new.auth.migrations import apply_all_migrations


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute durable PostgreSQL RAG refresh jobs")
    parser.add_argument("--once", action="store_true", help="process at most one job and exit")
    args = parser.parse_args()

    configure_logging()
    config = RAGPipelineConfig.from_settings()
    if config.vector_backend != "postgres" or not config.postgres_dsn:
        logging.getLogger(__name__).error("RAG refresh worker requires PostgreSQL vector backend and DSN")
        return 2
    apply_all_migrations(config.postgres_dsn)
    repository = PostgresRAGRefreshJobRepository(
        config.postgres_dsn,
        config.collection_name,
        lease_seconds=int(RAG_CONFIG.get("refresh_lease_seconds", 90)),
        max_attempts=int(RAG_CONFIG.get("refresh_max_attempts", 3)),
        retry_delay_seconds=int(RAG_CONFIG.get("refresh_retry_delay_seconds", 5)),
    )
    worker = RAGRefreshWorker(
        repository,
        config,
        poll_seconds=float(RAG_CONFIG.get("refresh_poll_seconds", 2.0)),
    )
    if args.once:
        worker.run_once()
    else:
        worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
