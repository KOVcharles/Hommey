"""PostgreSQL + pgvector implementation of the RAG VectorStore contract."""
from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from context.postgres_pool import get_postgres_pool

from .embedder import TextEmbedder, create_text_embedder
from .hybrid import hybrid_search
from .schemas import DocumentChunk, RetrievalResult
from .vector_store import VectorStore


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(item: Dict[str, Any]) -> RetrievalResult:
    return RetrievalResult(
        id=item.get("id"),
        content=item.get("content", ""),
        metadata=item.get("metadata") or {},
        distance=item.get("distance"),
        vector_rank=item.get("vector_rank"),
        bm25_rank=item.get("bm25_rank"),
        bm25_score=item.get("bm25_score"),
        fusion_score=float(item.get("fusion_score", 0.0) or 0.0),
        rerank_score=item.get("rerank_score"),
        retrieval_trace_id=item.get("retrieval_trace_id"),
    )


class PostgresVectorStore(VectorStore):
    """Versioned pgvector store with atomic active-version publication."""

    def __init__(
        self,
        *,
        postgres_dsn: str,
        collection_name: str,
        embedding_model: str,
        embedding_backend: str = "siliconflow",
        embedding_api_key: str | None = None,
        embedding_base_url: str = "https://api.siliconflow.cn/v1",
        embedding_dimension: int = 1024,
        embedding_batch_size: int = 32,
        embedding_timeout_sec: float = 30.0,
        embedding_max_retries: int = 2,
        embedding_retry_base_delay_sec: float = 1.0,
        embedding_retry_max_delay_sec: float = 30.0,
        embedding_cache_size: int = 1024,
        top_k: int = 3,
        vector_top_k: int = 10,
        bm25_top_k: int = 10,
        sparse_backend: str = "python",
        pool: Any | None = None,
        embedder: TextEmbedder | None = None,
    ):
        if not postgres_dsn and pool is None:
            raise ValueError("PostgreSQL RAG requires HOMMEY_RAG_POSTGRES_DSN or HOMMEY_POSTGRES_DSN")
        self.collection_name = collection_name
        self.embedding_model_name = embedding_model
        self.embedding_dimension = int(embedding_dimension)
        self.top_k = max(1, int(top_k))
        self.vector_top_k = max(1, int(vector_top_k))
        self.bm25_top_k = max(1, int(bm25_top_k))
        self.sparse_backend = (sparse_backend or "python").lower()
        self.pool = pool or get_postgres_pool(postgres_dsn)
        self.embedding_model = embedder or create_text_embedder(
            backend=embedding_backend,
            model=embedding_model,
            api_key=embedding_api_key,
            base_url=embedding_base_url,
            dimension=embedding_dimension,
            timeout_sec=embedding_timeout_sec,
            batch_size=embedding_batch_size,
            max_retries=embedding_max_retries,
            retry_base_delay_sec=embedding_retry_base_delay_sec,
            retry_max_delay_sec=embedding_retry_max_delay_sec,
            cache_size=embedding_cache_size,
        )
        actual_dimension = int(self.embedding_model.dimension())
        if actual_dimension != self.embedding_dimension:
            raise ValueError(
                f"embedding dimension mismatch: configured={self.embedding_dimension}, actual={actual_dimension}"
            )

    def _vector(self, values: List[float]) -> str:
        if len(values) != self.embedding_dimension:
            raise ValueError(
                f"embedding dimension mismatch: expected {self.embedding_dimension}, got {len(values)}"
            )
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("embedding contains non-finite values")
        return "[" + ",".join(format(float(value), ".12g") for value in values) + "]"

    @staticmethod
    def _fingerprint(chunks: List[DocumentChunk]) -> str:
        fingerprints = {str(chunk.index_version or "") for chunk in chunks}
        if len(fingerprints) != 1 or not next(iter(fingerprints), ""):
            raise ValueError("all chunks must carry one non-empty index fingerprint")
        return next(iter(fingerprints))

    def _prepare_rows(
        self, chunks: List[DocumentChunk], *, version: str, fingerprint: str,
    ) -> List[Dict[str, Any]]:
        embeddings = self.embedding_model.embed_texts([chunk.content for chunk in chunks])
        if len(embeddings) != len(chunks):
            raise RuntimeError("embedding service returned an incomplete vector batch")
        rows: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for chunk, embedding in zip(chunks, embeddings):
            metadata = chunk.to_metadata()
            chunk_id = str(metadata.get("chunk_id") or "")
            if not chunk_id or chunk_id in seen:
                raise ValueError(f"missing or duplicate chunk_id: {chunk_id!r}")
            seen.add(chunk_id)
            document_id = str(metadata.get("document_id") or "")
            document_version = str(metadata.get("document_version") or "")
            chunk_hash = str(metadata.get("chunk_hash") or metadata.get("hash") or "")
            if not (document_id and document_version and chunk_hash):
                raise ValueError(f"chunk {chunk_id} is missing durable lineage metadata")
            metadata.update(
                {
                    "index_fingerprint": fingerprint,
                    "index_version": version,
                    "collection_name": self.collection_name,
                    "ingested_at": _utc_now_iso(),
                }
            )
            rows.append(
                {
                    "chunk_id": chunk_id,
                    "document_id": document_id,
                    "document_version": document_version,
                    "chunk_hash": chunk_hash,
                    "content": chunk.content,
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                    "embedding": self._vector(embedding),
                }
            )
        return rows

    def _lock_collection(self, cursor) -> None:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"hommey:rag:{self.collection_name}",),
        )

    def _ensure_collection(self, cursor, fingerprint: str, schema_version: str) -> None:
        cursor.execute(
            """
            INSERT INTO rag_collections (
                collection_name, embedding_model, embedding_dimension,
                index_fingerprint, schema_version
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (collection_name) DO NOTHING
            """,
            (
                self.collection_name,
                self.embedding_model_name,
                self.embedding_dimension,
                fingerprint,
                schema_version,
            ),
        )

    def _insert_documents(self, cursor, version: str, rows: List[Dict[str, Any]]) -> None:
        documents: Dict[tuple[str, str], Dict[str, Any]] = {}
        for row in rows:
            metadata = json.loads(row["metadata"])
            key = (row["document_id"], row["document_version"])
            documents.setdefault(key, metadata)
        cursor.executemany(
            """
            INSERT INTO rag_documents (
                collection_name, index_version, document_id, document_version,
                source_path, source_hash, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (collection_name, index_version, document_id, document_version)
            DO UPDATE SET source_path=EXCLUDED.source_path,
                          source_hash=EXCLUDED.source_hash,
                          metadata=EXCLUDED.metadata
            """,
            [
                (
                    self.collection_name,
                    version,
                    document_id,
                    document_version,
                    metadata.get("source_path"),
                    metadata.get("hash"),
                    json.dumps(metadata, ensure_ascii=False),
                )
                for (document_id, document_version), metadata in documents.items()
            ],
        )

    def _insert_chunks(self, cursor, version: str, rows: List[Dict[str, Any]]) -> None:
        cursor.executemany(
            """
            INSERT INTO rag_chunks (
                collection_name, index_version, chunk_id, document_id,
                document_version, chunk_hash, content, metadata, embedding
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::vector)
            ON CONFLICT DO NOTHING
            """,
            [
                (
                    self.collection_name,
                    version,
                    row["chunk_id"],
                    row["document_id"],
                    row["document_version"],
                    row["chunk_hash"],
                    row["content"],
                    row["metadata"],
                    row["embedding"],
                )
                for row in rows
            ],
        )

    def replace_chunks(self, chunks: List[DocumentChunk]) -> Dict[str, Any]:
        if not chunks:
            raise ValueError("refusing to replace the knowledge base with zero chunks")
        fingerprint = self._fingerprint(chunks)
        version = f"{fingerprint}-{uuid.uuid4().hex[:12]}"
        schema_version = str(chunks[0].schema_version)
        rows = self._prepare_rows(chunks, version=version, fingerprint=fingerprint)

        with self.pool.connection() as connection, connection.cursor() as cursor:
            self._lock_collection(cursor)
            self._ensure_collection(cursor, fingerprint, schema_version)
            cursor.execute(
                """
                INSERT INTO rag_index_versions (
                    collection_name, version, status, index_fingerprint,
                    embedding_model, embedding_dimension, schema_version, build_config
                ) VALUES (%s, %s, 'building', %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    self.collection_name,
                    version,
                    fingerprint,
                    self.embedding_model_name,
                    self.embedding_dimension,
                    schema_version,
                    json.dumps({"source": "RAGPipeline.replace_chunks"}),
                ),
            )
            self._insert_documents(cursor, version, rows)
            self._insert_chunks(cursor, version, rows)
            cursor.execute(
                "SELECT COUNT(*) AS count FROM rag_chunks WHERE collection_name=%s AND index_version=%s",
                (self.collection_name, version),
            )
            staged = int(cursor.fetchone()["count"])
            if staged != len(rows):
                raise RuntimeError(f"staged RAG verification failed: expected {len(rows)}, got {staged}")
            cursor.execute(
                """
                UPDATE rag_index_versions
                SET status='retired', retired_at=NOW()
                WHERE collection_name=%s AND status='active'
                """,
                (self.collection_name,),
            )
            cursor.execute(
                """
                UPDATE rag_index_versions
                SET status='active', activated_at=NOW(), chunk_count=%s
                WHERE collection_name=%s AND version=%s AND status='building'
                """,
                (staged, self.collection_name, version),
            )
            cursor.execute(
                """
                UPDATE rag_collections
                SET active_version=%s, embedding_model=%s, embedding_dimension=%s,
                    index_fingerprint=%s, schema_version=%s, updated_at=NOW()
                WHERE collection_name=%s
                """,
                (
                    version,
                    self.embedding_model_name,
                    self.embedding_dimension,
                    fingerprint,
                    schema_version,
                    self.collection_name,
                ),
            )
        return {
            "status": "success",
            "added_count": len(rows),
            "total_count": len(rows),
            "index_version": version,
            "index_fingerprint": fingerprint,
        }

    def add_chunks(self, chunks: List[DocumentChunk]) -> Dict[str, Any]:
        if not chunks:
            return {"status": "success", "added_count": 0, "total_count": self.count()}
        fingerprint = self._fingerprint(chunks)
        with self.pool.connection() as connection, connection.cursor() as cursor:
            self._lock_collection(cursor)
            cursor.execute(
                """
                SELECT active_version, index_fingerprint
                FROM rag_collections WHERE collection_name=%s
                """,
                (self.collection_name,),
            )
            state = cursor.fetchone()
        if not state or not state["active_version"]:
            return self.replace_chunks(chunks)
        if state["index_fingerprint"] != fingerprint:
            raise ValueError("RAG index fingerprint changed; run a full rebuild")
        version = str(state["active_version"])
        rows = self._prepare_rows(chunks, version=version, fingerprint=fingerprint)
        fresh_versions: Dict[str, set[str]] = {}
        for row in rows:
            fresh_versions.setdefault(row["document_id"], set()).add(row["document_version"])

        with self.pool.connection() as connection, connection.cursor() as cursor:
            self._lock_collection(cursor)
            cursor.execute(
                "SELECT active_version FROM rag_collections WHERE collection_name=%s FOR UPDATE",
                (self.collection_name,),
            )
            current = cursor.fetchone()
            if not current or current["active_version"] != version:
                raise RuntimeError("active RAG version changed during incremental ingestion")
            cursor.execute(
                "SELECT COUNT(*) AS count FROM rag_chunks WHERE collection_name=%s AND index_version=%s",
                (self.collection_name, version),
            )
            before = int(cursor.fetchone()["count"])
            self._insert_documents(cursor, version, rows)
            self._insert_chunks(cursor, version, rows)
            for document_id, versions in fresh_versions.items():
                cursor.execute(
                    """
                    DELETE FROM rag_chunks
                    WHERE collection_name=%s AND index_version=%s AND document_id=%s
                      AND NOT (document_version = ANY(%s))
                    """,
                    (self.collection_name, version, document_id, list(versions)),
                )
                cursor.execute(
                    """
                    DELETE FROM rag_documents
                    WHERE collection_name=%s AND index_version=%s AND document_id=%s
                      AND NOT (document_version = ANY(%s))
                    """,
                    (self.collection_name, version, document_id, list(versions)),
                )
            cursor.execute(
                "SELECT COUNT(*) AS count FROM rag_chunks WHERE collection_name=%s AND index_version=%s",
                (self.collection_name, version),
            )
            total = int(cursor.fetchone()["count"])
            cursor.execute(
                """
                UPDATE rag_index_versions SET chunk_count=%s
                WHERE collection_name=%s AND version=%s
                """,
                (total, self.collection_name, version),
            )
        return {"status": "success", "added_count": max(0, total - before), "total_count": total}

    def vector_search(self, query: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        vector = self._vector(self.embedding_model.embed_query(query))
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.chunk_id AS id, c.content, c.metadata,
                       c.embedding <=> %s::vector AS distance
                FROM rag_chunks c
                JOIN rag_collections r
                  ON r.collection_name=c.collection_name AND r.active_version=c.index_version
                WHERE c.collection_name=%s
                ORDER BY c.embedding <=> %s::vector
                LIMIT %s
                """,
                (vector, self.collection_name, vector, int(top_k or self.vector_top_k)),
            )
            rows = cursor.fetchall()
        return [
            {
                "id": row["id"],
                "content": row["content"],
                "metadata": row["metadata"],
                "distance": float(row["distance"]),
                "vector_rank": rank,
            }
            for rank, row in enumerate(rows, start=1)
        ]

    def fetch_all_documents(self) -> List[Dict[str, Any]]:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.chunk_id AS id, c.content, c.metadata
                FROM rag_chunks c
                JOIN rag_collections r
                  ON r.collection_name=c.collection_name AND r.active_version=c.index_version
                WHERE c.collection_name=%s
                ORDER BY c.chunk_id
                """,
                (self.collection_name,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def search(self, query: str, top_k: Optional[int] = None) -> List[RetrievalResult]:
        return [_result(item) for item in hybrid_search(self, query, top_k)]

    def search_dense(self, query: str, top_k: Optional[int] = None) -> List[RetrievalResult]:
        return [_result(item) for item in self.vector_search(query, top_k)]

    def count(self) -> int:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM rag_chunks c JOIN rag_collections r
                  ON r.collection_name=c.collection_name AND r.active_version=c.index_version
                WHERE c.collection_name=%s
                """,
                (self.collection_name,),
            )
            return int(cursor.fetchone()["count"])

    def stats(self) -> Dict[str, Any]:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT active_version, index_fingerprint, embedding_model,
                       embedding_dimension, schema_version
                FROM rag_collections WHERE collection_name=%s
                """,
                (self.collection_name,),
            )
            row = cursor.fetchone()
        return {
            "status": "success",
            "backend": "postgres",
            "collection_name": self.collection_name,
            "total_documents": self.count() if row and row["active_version"] else 0,
            "index_version": row["active_version"] if row else None,
            "index_fingerprint": row["index_fingerprint"] if row else None,
            "embedding_model": row["embedding_model"] if row else self.embedding_model_name,
            "embedding_dimension": row["embedding_dimension"] if row else self.embedding_dimension,
            "schema_version": row["schema_version"] if row else None,
        }

    def rebuild(self) -> None:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            self._lock_collection(cursor)
            cursor.execute(
                "UPDATE rag_index_versions SET status='retired', retired_at=NOW() WHERE collection_name=%s AND status='active'",
                (self.collection_name,),
            )
            cursor.execute(
                "UPDATE rag_collections SET active_version=NULL, updated_at=NOW() WHERE collection_name=%s",
                (self.collection_name,),
            )
