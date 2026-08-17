"""Milvus Lite storage wrapper for the RAG pipeline."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path, PosixPath
from typing import Any, Dict, List, Optional

from .embedder import create_text_embedder
from .schemas import KnowledgeChunk, RetrievalResult

logger = logging.getLogger(__name__)
_EMBEDDING_MODEL_CACHE: Dict[str, Any] = {}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _idempotency_key(chunk: KnowledgeChunk) -> tuple[str, str, str]:
    """Idempotency key = chunk_hash + document_id + document_version (audit P6)."""
    metadata = chunk.to_metadata()
    return (
        str(metadata.get("chunk_hash") or ""),
        str(metadata.get("document_id") or ""),
        str(metadata.get("document_version") or ""),
    )


def _stable_row_id(chunk: KnowledgeChunk) -> int:
    """Derive a positive Int64 primary key from stable chunk lineage.

    Row-count-based ids are unsafe after deletions: once a document version is
    retired, ``count + 1`` can reuse an id that still belongs to another row.
    A lineage-derived id is stable across retries and independent of collection
    gaps, which also lets Milvus ``upsert`` make concurrent identical writes
    idempotent.
    """
    metadata = chunk.to_metadata()
    identity = str(metadata.get("chunk_id") or "")
    if not identity:
        identity = json.dumps(_idempotency_key(chunk), ensure_ascii=False, separators=(",", ":"))
    row_id = int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:8], "big")
    return (row_id & ((1 << 63) - 1)) or 1


def _fresh_versions(chunks: List[KnowledgeChunk]) -> Dict[str, set[str]]:
    """document_id -> set(document_version) being written by this batch.

    The version-retirement pass (audit §4.3: 新版本写入 → 校验 → 按 doc_id 切换
    版本) keeps a changed document's newest rows and removes superseded ones,
    so incremental refresh never accumulates duplicate versions.
    """
    versions: Dict[str, set[str]] = {}
    for chunk in chunks:
        metadata = chunk.to_metadata()
        document_id = str(metadata.get("document_id") or "")
        document_version = str(metadata.get("document_version") or "")
        if document_id:
            versions.setdefault(document_id, set()).add(document_version)
    return versions
_DOMAIN_TERMS = (
    "差旅申请",
    "住宿标准",
    "住宿费",
    "交通费",
    "打车费",
    "机票",
    "火车票",
    "餐补",
    "餐费",
    "餐饮",
    "早餐",
    "午餐",
    "晚餐",
    "业务招待",
    "个人零食",
    "饮料",
    "酒水",
    "报销",
    "不予报销",
    "发票",
    "补贴",
    "国际出差",
    "国内出差",
)

_GRPC_MAX_MS = "2147483647"
os.environ.setdefault("GRPC_KEEPALIVE_TIME_MS", _GRPC_MAX_MS)
os.environ.setdefault("GRPC_KEEPALIVE_TIMEOUT_MS", "20000")
os.environ.setdefault("GRPC_KEEPALIVE_PERMIT_WITHOUT_CALLS", "0")
os.environ.setdefault("GRPC_HTTP2_MIN_RECV_PING_INTERVAL_WITHOUT_DATA_MS", _GRPC_MAX_MS)
os.environ.setdefault("GRPC_HTTP2_MIN_PING_INTERVAL_WITHOUT_DATA_MS", _GRPC_MAX_MS)

try:
    from pymilvus import MilvusClient

    DEPENDENCIES_AVAILABLE = True
except ImportError as exc:
    logger.warning("RAG dependencies are not available: %s", exc)
    DEPENDENCIES_AVAILABLE = False


def resolve_embedding_model(model_name_or_path: str) -> str:
    path = Path(model_name_or_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.exists():
        return str(path.resolve())
    return model_name_or_path


def resolve_milvus_uri(knowledge_base_path: str) -> str:
    base = Path(knowledge_base_path)
    if base.suffix == ".db":
        if base.exists() and base.is_file():
            return str(base.parent / "milvus_lite_v2.db")
        return str(base)
    default_db = base / "milvus_lite.db"
    if default_db.exists() and default_db.is_file():
        return str(base / "milvus_lite_v2.db")
    return str(default_db)


def _resolve_local_path(path: str | Path) -> Path:
    """Resolve paths even when tests monkeypatch os.name to simulate Windows."""
    try:
        return Path(path).resolve()
    except Exception as exc:
        if "cannot instantiate" not in str(exc) or "WindowsPath" not in str(exc):
            raise
        return PosixPath(path).resolve()


class MilvusKnowledgeStore:
    """Owns embedding, Milvus writes, vector search, and local keyword search."""

    def __init__(
        self,
        knowledge_base_path: str,
        collection_name: str,
        embedding_model: str,
        embedding_backend: str = "siliconflow",
        embedding_api_key: str | None = None,
        embedding_base_url: str = "https://api.siliconflow.cn/v1",
        embedding_dimension: int = 1024,
        embedding_timeout_sec: float = 30.0,
        embedding_batch_size: int = 32,
        # Phase 5 (audit §4.15): bounded embedding retry/backoff + process-local
        # cache so transient API failures and repeated texts don't stall or
        # re-pay the embedding call.
        embedding_max_retries: int = 2,
        embedding_retry_base_delay_sec: float = 1.0,
        embedding_retry_max_delay_sec: float = 30.0,
        embedding_cache_size: int = 1024,
        top_k: int = 3,
        vector_top_k: int = 10,
        bm25_top_k: int = 10,
        # Phase 5: BM25/sparse backend selection (audit §11 Phase 5, non-scheduled
        # until the corpus outgrows the Python full-scan).  Only "python" today.
        sparse_backend: str = "python",
    ):
        if not DEPENDENCIES_AVAILABLE:
            raise RuntimeError("RAG dependencies not installed: pymilvus, milvus-lite")

        self.knowledge_base_path = Path(knowledge_base_path)
        self.knowledge_base_path.mkdir(parents=True, exist_ok=True)
        self.collection_name = collection_name
        self.top_k = top_k
        self.vector_top_k = vector_top_k
        self.bm25_top_k = bm25_top_k
        self.sparse_backend = (sparse_backend or "python").lower()

        model_name = resolve_embedding_model(embedding_model) if embedding_backend == "local" else embedding_model
        self.embedding_model = create_text_embedder(
            backend=embedding_backend,
            model=model_name,
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
        self.embedding_dim = self.embedding_model.dimension()

        self.milvus_uri = resolve_milvus_uri(str(self.knowledge_base_path))
        self.grpc_options = {
            "keepalive_time": _GRPC_MAX_MS,
            "keepalive_timeout": "20000",
            "keepalive_permit_without_calls": "0",
            "http2_min_recv_ping_interval_without_data": _GRPC_MAX_MS,
            "http2_min_ping_interval_without_data": _GRPC_MAX_MS,
        }
        self.client = self._new_client()
        self.ensure_collection()

    def _new_client(self):
        # pymilvus 在 db_name 为空时会从 uri 路径首段推断 database 名（如
        # /app/data/... 会推断成 "app"），导致 Milvus Lite 报
        # "database 'app' does not exist"。显式指定 default database。
        return MilvusClient(
            self.milvus_uri,
            grpc_options=self.grpc_options,
            db_name="default",
        )

    def _reset_client(self) -> None:
        self.close()
        self.client = self._new_client()

    def ensure_collection(self) -> None:
        if not self.client.has_collection(self.collection_name):
            self.client.create_collection(
                collection_name=self.collection_name,
                dimension=self.embedding_dim,
                metric_type="COSINE",
                auto_id=False,
            )
        self.load_collection()

    def rebuild_collection(self) -> None:
        if self.client.has_collection(self.collection_name):
            self._prepare_windows_manifest_replace()
            try:
                self.client.drop_collection(self.collection_name)
            except Exception as exc:
                if not _is_windows_manifest_replace_error(exc):
                    raise
                logger.warning(
                    "Milvus Lite drop_collection hit a Windows manifest replace error; "
                    "falling back to local collection directory cleanup for rebuild."
                )
                self._cleanup_local_collection_dir()
                self._reset_client()
                if self.client.has_collection(self.collection_name):
                    self.client.drop_collection(self.collection_name)
        self.client.create_collection(
            collection_name=self.collection_name,
            dimension=self.embedding_dim,
            metric_type="COSINE",
            auto_id=False,
        )
        self.load_collection()

    def _cleanup_local_collection_dir(self) -> None:
        db_root = _resolve_local_path(self.milvus_uri)
        collection_dir = _resolve_local_path(db_root / "collections" / self.collection_name)
        if not collection_dir.is_relative_to(db_root):
            raise RuntimeError(f"Refusing to clean collection outside Milvus root: {collection_dir}")
        if collection_dir.exists():
            shutil.rmtree(collection_dir)

    def _prepare_windows_manifest_replace(self) -> None:
        if os.name != "nt":
            return
        db_root = _resolve_local_path(self.milvus_uri)
        manifest_path = _resolve_local_path(db_root / "collections" / self.collection_name / "manifest.json")
        if not manifest_path.is_relative_to(db_root):
            raise RuntimeError(f"Refusing to edit manifest outside Milvus root: {manifest_path}")
        if manifest_path.exists():
            manifest_path.unlink()

    def load_collection(self) -> None:
        try:
            self.client.load_collection(self.collection_name)
        except Exception as exc:
            logger.debug("Milvus collection load skipped or failed: %s", exc)

    def add_chunks(self, chunks: List[KnowledgeChunk]) -> Dict[str, Any]:
        if not chunks:
            return {"status": "success", "added_count": 0, "total_count": self.count()}

        # Idempotent upsert (audit §4.3 / §7 P6): a chunk already present under
        # the same (chunk_hash, document_id, document_version) key is skipped so
        # re-running an incremental write adds zero new rows.
        existing_keys = self._idempotency_keys()
        fresh: List[KnowledgeChunk] = []
        for chunk in chunks:
            key = _idempotency_key(chunk)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            fresh.append(chunk)

        if not fresh:
            return {"status": "success", "added_count": 0, "total_count": self.count()}

        rows = []
        row_ids: set[int] = set()
        for chunk in fresh:
            doc_id = _stable_row_id(chunk)
            if doc_id in row_ids:
                raise RuntimeError("stable Milvus row id collision within ingestion batch")
            row_ids.add(doc_id)
            metadata = chunk.to_metadata()
            metadata["ingested_at"] = _utc_now_iso()
            metadata["collection_name"] = self.collection_name
            rows.append(
                {
                    "id": doc_id,
                    "content": chunk.content,
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                }
            )
        vectors = self.embedding_model.embed_texts([chunk.content for chunk in fresh])
        for row, vector in zip(rows, vectors):
            row["vector"] = vector

        writer = getattr(self.client, "upsert", self.client.insert)
        writer(collection_name=self.collection_name, data=rows)
        self.load_collection()
        # The new version is verified on disk; now retire the superseded rows
        # of each changed document (audit §4.3: 先 hash 幂等，后按 doc_id 切换版本).
        self._retire_superseded_versions(fresh)
        return {"status": "success", "added_count": len(rows), "total_count": self.count()}

    def _retire_superseded_versions(self, fresh: List[KnowledgeChunk]) -> int:
        """Delete rows of a changed document whose version is older than the one
        just written, so incremental refresh does not accumulate duplicates."""
        fresh_versions = _fresh_versions(fresh)
        if not fresh_versions:
            return 0
        retired = 0
        for doc in self.fetch_all_documents():
            metadata = doc.get("metadata") or {}
            document_id = str(metadata.get("document_id") or "")
            if document_id not in fresh_versions:
                continue
            document_version = str(metadata.get("document_version") or "")
            if document_version in fresh_versions[document_id]:
                continue
            row_id = doc.get("id")
            if row_id is None:
                continue
            self.client.delete(collection_name=self.collection_name, ids=[row_id])
            retired += 1
        if retired:
            self.load_collection()
        return retired

    def _idempotency_keys(self) -> set[tuple[str, str, str]]:
        """Collect idempotency keys of already-indexed chunks for dedup."""
        keys: set[tuple[str, str, str]] = set()
        for doc in self.fetch_all_documents():
            metadata = doc.get("metadata") or {}
            chunk_hash = str(metadata.get("chunk_hash") or "")
            document_id = str(metadata.get("document_id") or "")
            document_version = str(metadata.get("document_version") or "")
            if chunk_hash or document_id:
                keys.add((chunk_hash, document_id, document_version))
        return keys

    def replace_chunks_atomically(self, chunks: List[KnowledgeChunk]) -> Dict[str, Any]:
        """Blue/green replacement with rollback-safe collection renames.

        Embeddings are produced before any live collection is touched.  A
        staging collection is then populated and verified.  Only after that do
        we rename the live collection to a backup and promote staging.  If the
        promotion fails, the backup is immediately restored.
        """
        if not chunks:
            raise ValueError("refusing to replace the knowledge base with zero chunks")

        vectors = self.embedding_model.embed_texts([chunk.content for chunk in chunks])
        if len(vectors) != len(chunks):
            raise RuntimeError("embedding service returned an incomplete vector batch")

        suffix = uuid.uuid4().hex[:10]
        staging = f"{self.collection_name}__staging_{suffix}"
        backup = f"{self.collection_name}__backup_{suffix}"
        rows = []
        row_ids: set[int] = set()
        for chunk, vector in zip(chunks, vectors):
            doc_id = _stable_row_id(chunk)
            if doc_id in row_ids:
                raise RuntimeError("stable Milvus row id collision within replacement batch")
            row_ids.add(doc_id)
            metadata = chunk.to_metadata()
            metadata["ingested_at"] = _utc_now_iso()
            metadata["collection_name"] = self.collection_name
            rows.append(
                {
                    "id": doc_id,
                    "content": chunk.content,
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                    "vector": vector,
                }
            )

        live_moved = False
        promoted = False
        try:
            self.client.create_collection(
                collection_name=staging,
                dimension=self.embedding_dim,
                metric_type="COSINE",
                auto_id=False,
            )
            self.client.insert(collection_name=staging, data=rows)
            staged_count = int(
                self.client.get_collection_stats(staging).get("row_count", 0) or 0
            )
            if staged_count != len(rows):
                raise RuntimeError(
                    f"staging collection verification failed: expected {len(rows)}, got {staged_count}"
                )

            if self.client.has_collection(self.collection_name):
                self.client.rename_collection(self.collection_name, backup)
                live_moved = True
            try:
                self.client.rename_collection(staging, self.collection_name)
                promoted = True
            except Exception:
                if self.client.has_collection(self.collection_name):
                    # The promote rename actually landed server-side but surfaced
                    # as an error (e.g. timeout).  The new index is live — treat
                    # it as promoted so the manifest tracks reality instead of
                    # leaving the "index changed but manifest stale" state
                    # (§6.1.5) with an orphaned backup.
                    logger.warning(
                        "staging promote raised but live collection %s exists; assuming promoted",
                        self.collection_name,
                    )
                    promoted = True
                elif live_moved and self.client.has_collection(backup):
                    self.client.rename_collection(backup, self.collection_name)
                    live_moved = False
                    raise
                else:
                    raise

            self.load_collection()
            if live_moved and self.client.has_collection(backup):
                try:
                    self.client.drop_collection(backup)
                except Exception:
                    # Promotion already succeeded.  A stale backup is harmless
                    # and must not turn a completed refresh into a false error.
                    logger.warning("Unable to remove retired RAG backup collection %s", backup)
            return {
                "status": "success",
                "added_count": len(rows),
                "total_count": len(rows),
            }
        finally:
            if not promoted and self.client.has_collection(staging):
                self.client.drop_collection(staging)

    def vector_search(self, query: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        self.load_collection()
        query_embedding = self.embedding_model.embed_query(query)
        results = self.client.search(
            collection_name=self.collection_name,
            data=[query_embedding],
            limit=top_k or self.vector_top_k,
            output_fields=["id", "content", "metadata"],
        )
        docs: List[Dict[str, Any]] = []
        for rank, hit in enumerate(results[0] if results else [], start=1):
            entity = hit.get("entity", {})
            docs.append(
                {
                    "id": entity.get("id", hit.get("id", "")),
                    "content": entity.get("content", ""),
                    "metadata": _loads_metadata(entity.get("metadata", "{}")),
                    "distance": hit.get("distance", 0.0),
                    "vector_rank": rank,
                }
            )
        return docs

    def fetch_all_documents(self) -> List[Dict[str, Any]]:
        total = self.count()
        if total <= 0:
            return []

        self.load_collection()
        rows: List[Dict[str, Any]] = []
        chunk_size = 500
        # Paginate with offset, not an id range: ids are not guaranteed
        # contiguous once chunks are deduplicated or deleted (audit §4.9).
        for offset in range(0, total, chunk_size):
            rows.extend(
                self.client.query(
                    collection_name=self.collection_name,
                    filter="",
                    offset=offset,
                    limit=chunk_size,
                    output_fields=["id", "content", "metadata"],
                )
            )

        return [
            {
                "id": row.get("id", ""),
                "content": row.get("content", ""),
                "metadata": _loads_metadata(row.get("metadata", "{}")),
            }
            for row in rows
        ]

    def bm25_search(self, query: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        # Phase 5: keyword ranking routes through the SparseIndex extension
        # point (rag/sparse.py).  The Python backend keeps the previous full-scan
        # behavior and scores exactly; a future native-sparse backend plugs in
        # via HOMMEY_RAG_BM25_BACKEND without touching hybrid_search/fusion.
        # Imported lazily to avoid a module cycle (sparse imports _tokenize here).
        from .hybrid import bm25_search

        return bm25_search(self, query, top_k)

    def hybrid_search(self, query: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        from .hybrid import hybrid_search

        return hybrid_search(self, query, top_k)

    def count(self) -> int:
        stats = self.client.get_collection_stats(self.collection_name)
        return int(stats.get("row_count", 0))

    def stats(self) -> Dict[str, Any]:
        return {
            "status": "success",
            "collection_name": self.collection_name,
            "total_documents": self.count(),
            "knowledge_base_path": str(self.knowledge_base_path),
            "milvus_uri": self.milvus_uri,
        }

    def close(self) -> None:
        if hasattr(self.client, "close"):
            self.client.close()


def fuse_results(vector_docs: List[Dict[str, Any]], bm25_docs: List[Dict[str, Any]], top_k: int) -> List[Dict[str, Any]]:
    rrf_k = 60.0
    merged: Dict[str, Dict[str, Any]] = {}

    for rank, doc in enumerate(vector_docs, start=1):
        key = str(doc.get("id"))
        merged[key] = {
            "id": doc.get("id", ""),
            "content": doc.get("content", ""),
            "metadata": doc.get("metadata", {}),
            "distance": doc.get("distance"),
            "vector_rank": rank,
            "bm25_rank": None,
            "fusion_score": 1.0 / (rrf_k + rank),
        }

    for rank, doc in enumerate(bm25_docs, start=1):
        key = str(doc.get("id"))
        if key not in merged:
            merged[key] = {
                "id": doc.get("id", ""),
                "content": doc.get("content", ""),
                "metadata": doc.get("metadata", {}),
                "distance": None,
                "vector_rank": None,
                "bm25_rank": rank,
                "fusion_score": 0.0,
            }
        merged[key]["bm25_rank"] = rank
        merged[key]["bm25_score"] = doc.get("bm25_score")
        merged[key]["fusion_score"] += 1.0 / (rrf_k + rank)

    return sorted(merged.values(), key=lambda doc: doc["fusion_score"], reverse=True)[:top_k]


def _is_windows_manifest_replace_error(exc: Exception) -> bool:
    message = str(exc)
    return (
        "WinError 183" in message
        and "manifest.json.tmp" in message
        and "manifest.json" in message
    )


def rerank_results(docs: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    terms = _rerank_terms(query)
    query_ngrams = _query_ngrams(query)
    focus_terms = _focus_terms(query)
    ngram_df = {
        ngram: sum(1 for doc in docs if ngram in doc.get("content", ""))
        for ngram in query_ngrams
    }
    if not terms:
        terms = []

    def score(doc: Dict[str, Any]) -> float:
        content = doc.get("content", "")
        matches = sum(1 for term in terms if term in content)
        ngram_bonus = sum(
            0.015 * (1.0 + math.log((len(docs) + 1.0) / (ngram_df[term] + 1.0)))
            for term in query_ngrams
            if term in content
        )
        focus_bonus = sum(
            (0.30 if index == 0 else 0.18)
            for index, term in enumerate(focus_terms)
            if term in content
        )
        title = str((doc.get("metadata") or {}).get("title", ""))
        title_matches = sum(1 for term in terms if term in title)
        penalty = _off_topic_penalty(query, content)
        return (
            float(doc.get("fusion_score", 0.0))
            + matches * 0.04
            + title_matches * 0.02
            + min(ngram_bonus, 0.30)
            + focus_bonus
            - penalty
        )

    for doc in docs:
        doc["rerank_score"] = score(doc)
    return sorted(docs, key=lambda doc: doc.get("rerank_score", 0.0), reverse=True)


def filter_relevant_results(docs: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """Domain-term relevance filter.

    The audit (§9.4) drops the "rank-1 unconditional pass" — an off-topic doc
    ranked first by a single branch must not survive purely because of its rank.
    The filter stays off for queries with no domain terms, keeping generic
    queries byte-identical to the unfiltered path.
    """
    terms = _rerank_terms(query)
    if not terms:
        return docs

    return [
        doc
        for doc in docs
        if any(term in doc.get("content", "") for term in terms)
    ]


def _rerank_terms(query: str) -> List[str]:
    terms = [term for term in ("餐补", "餐费", "餐饮", "早餐", "午餐", "晚餐", "报销", "个人零食", "酒水") if term in query]
    if any(term in query for term in ("餐补", "饭补", "吃饭")):
        terms.extend(["餐费", "餐饮", "早餐", "午餐", "晚餐", "报销"])
    if any(term in query for term in ("住宿", "酒店", "房费")):
        terms.extend(["住宿标准", "住宿费", "住宿上限", "酒店"])
    return list(dict.fromkeys(terms))


def _off_topic_penalty(query: str, content: str) -> float:
    if not any(term in query for term in ("餐补", "餐费", "餐饮", "饭补", "吃饭")):
        return 0.0

    meal_terms = ("餐费", "餐饮", "早餐", "午餐", "晚餐", "业务招待", "个人零食", "饮料", "酒水")
    meal_matches = sum(1 for term in meal_terms if term in content)
    international_query = any(
        term in query
        for term in ("国际", "境外", "国外", "港澳", "新加坡", "日本", "韩国", "美国", "加拿大", "英国", "法国", "德国", "澳大利亚", "阿联酋")
    )
    unrelated_terms = ["家属", "升级酒店", "升级机票", "签证", "护照", "里程", "积分"]
    unrelated_terms.append("国内出差" if international_query else "国际出差")
    unrelated_matches = sum(1 for term in unrelated_terms if term in content)
    if meal_matches >= 2:
        return 0.0
    return unrelated_matches * 0.03


def _query_ngrams(text: str) -> List[str]:
    """Return longer Chinese n-grams that reward exact concepts at rerank time."""
    runs = re.findall(r"[\u4e00-\u9fff]+", (text or "").lower())
    return list(
        dict.fromkeys(
            run[start : start + width]
            for run in runs
            for width in (3, 4)
            for start in range(0, len(run) - width + 1)
        )
    )


def _focus_terms(text: str) -> List[str]:
    """Extract the concrete subject after removing generic policy wording."""
    normalized = (text or "").lower()
    boilerplate = (
        "是多少",
        "有没有",
        "出差期间",
        "出差途中",
        "是否可以报销",
        "可以报销吗",
        "是否可报销",
        "能不能报销",
        "能否报销",
        "可以报销",
        "报销吗",
        "出差",
        "差旅",
        "标准",
        "多少",
        "怎么",
        "如何",
        "怎样",
        "什么",
        "哪些",
        "是否",
        "能否",
        "可以",
        "报销",
        "请问",
    )
    for phrase in boilerplate:
        normalized = normalized.replace(phrase, " ")
    generic = {"费用", "标准", "流程", "规定", "政策", "员工", "公司"}
    return [
        run
        for run in re.findall(r"[\u4e00-\u9fff]+", normalized)
        if len(run) >= 2 and run not in generic
    ]


def _get_embedding_model(model_path: str):
    cached = _EMBEDDING_MODEL_CACHE.get(model_path)
    if cached is not None:
        return cached

    from sentence_transformers import SentenceTransformer
    from sentence_transformers import models as st_models

    local_path = Path(model_path)
    if local_path.exists() and not (local_path / "modules.json").exists():
        transformer = st_models.Transformer(model_path, model_args={"local_files_only": True})
        pooling = st_models.Pooling(
            transformer.get_word_embedding_dimension(),
            pooling_mode_mean_tokens=True,
        )
        model = SentenceTransformer(modules=[transformer, pooling])
    elif local_path.exists():
        model = SentenceTransformer(model_path, local_files_only=True)
    else:
        model = SentenceTransformer(model_path)
    _EMBEDDING_MODEL_CACHE[model_path] = model
    return model


def _loads_metadata(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except Exception:
        return {}


def _tokenize(text: str) -> List[str]:
    text = (text or "").lower()
    word_tokens = re.findall(r"[a-z0-9_]+", text)
    phrase_tokens = [term.lower() for term in _DOMAIN_TERMS if term.lower() in text]
    zh_runs = re.findall(r"[\u4e00-\u9fff]+", text)
    zh_tokens = [char for run in zh_runs for char in run]
    # Single-character tokenization has high recall but gives generic questions
    # such as “某费用可以报销吗” nearly identical BM25 scores.  Short n-grams
    # preserve exact Chinese concepts (for example “宠物寄养费”) while keeping
    # the implementation dependency-free.  Never bridge punctuation or lines.
    zh_ngrams = [
        run[start : start + width]
        for run in zh_runs
        for width in (2, 3, 4)
        for start in range(0, len(run) - width + 1)
    ]
    return word_tokens + phrase_tokens + zh_tokens + zh_ngrams


# Compatibility re-exports: callers historically imported ranking helpers from
# this module.  Runtime binding now points every backend to the shared module.
from .ranking import (  # noqa: E402,F401
    _tokenize as _tokenize,
    filter_relevant_results as filter_relevant_results,
    fuse_results as fuse_results,
    rerank_results as rerank_results,
)
