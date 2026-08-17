"""One-shot Milvus Lite to PostgreSQL/pgvector RAG migration."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from rag.config import RAGPipelineConfig
from rag.retriever import _coerce_chunk
from rag.vector_store import MilvusVectorStore
from rag.postgres_vector_store import PostgresVectorStore
from webui_new.auth.migrations import apply_all_migrations


def migrate(*, dry_run: bool = False) -> dict:
    config = RAGPipelineConfig.from_settings()
    if not config.postgres_dsn:
        raise RuntimeError("HOMMEY_RAG_POSTGRES_DSN or HOMMEY_POSTGRES_DSN is required")
    source = MilvusVectorStore(
        knowledge_base_path=config.knowledge_base_path,
        collection_name=config.collection_name,
        embedding_model=config.embedding_model,
        embedding_backend=config.embedding_backend,
        embedding_api_key=config.embedding_api_key,
        embedding_base_url=config.embedding_base_url,
        embedding_dimension=config.embedding_dimension,
        embedding_batch_size=config.embedding_batch_size,
        embedding_timeout_sec=config.embedding_timeout_sec,
        embedding_max_retries=config.embedding_max_retries,
        embedding_retry_base_delay_sec=config.embedding_retry_base_delay_sec,
        embedding_retry_max_delay_sec=config.embedding_retry_max_delay_sec,
        embedding_cache_size=config.embedding_cache_size,
        top_k=config.top_k,
        vector_top_k=config.vector_top_k,
        bm25_top_k=config.bm25_top_k,
        sparse_backend=config.bm25_backend,
    )
    try:
        documents = source.store.fetch_all_documents()
        chunks = [_coerce_chunk(document, index) for index, document in enumerate(documents, start=1)]
        if not chunks:
            raise RuntimeError("Milvus Lite source collection is empty")
        fingerprints = {chunk.index_version for chunk in chunks if chunk.index_version}
        if len(fingerprints) != 1:
            raise RuntimeError("Milvus source does not contain one consistent index fingerprint")
        if dry_run:
            return {
                "status": "dry_run",
                "collection_name": config.collection_name,
                "source_count": len(chunks),
                "index_fingerprint": next(iter(fingerprints)),
            }

        apply_all_migrations(config.postgres_dsn)
        target = PostgresVectorStore(
            postgres_dsn=config.postgres_dsn,
            collection_name=config.collection_name,
            embedding_model=config.embedding_model,
            embedding_backend=config.embedding_backend,
            embedding_api_key=config.embedding_api_key,
            embedding_base_url=config.embedding_base_url,
            embedding_dimension=config.embedding_dimension,
            embedding_batch_size=config.embedding_batch_size,
            embedding_timeout_sec=config.embedding_timeout_sec,
            embedding_max_retries=config.embedding_max_retries,
            embedding_retry_base_delay_sec=config.embedding_retry_base_delay_sec,
            embedding_retry_max_delay_sec=config.embedding_retry_max_delay_sec,
            embedding_cache_size=config.embedding_cache_size,
            top_k=config.top_k,
            vector_top_k=config.vector_top_k,
            bm25_top_k=config.bm25_top_k,
            sparse_backend=config.bm25_backend,
        )
        result = target.replace_chunks(chunks)
        stats = target.stats()
        if int(stats.get("total_documents") or 0) != len(chunks):
            raise RuntimeError("PostgreSQL target count does not match Milvus source")
        return {
            "status": "success",
            "collection_name": config.collection_name,
            "source_count": len(chunks),
            "target_count": stats["total_documents"],
            "index_version": result["index_version"],
            "index_fingerprint": result["index_fingerprint"],
        }
    finally:
        source.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="inspect source without writing PostgreSQL")
    args = parser.parse_args()
    print(migrate(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
