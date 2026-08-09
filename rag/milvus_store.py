"""Milvus Lite storage wrapper for the RAG pipeline."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import time
import uuid
from pathlib import Path, PosixPath
from typing import Any, Dict, List, Optional

from .embedder import create_text_embedder
from .schemas import KnowledgeChunk, RetrievalResult

logger = logging.getLogger(__name__)
_EMBEDDING_MODEL_CACHE: Dict[str, Any] = {}
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
        top_k: int = 3,
        vector_top_k: int = 10,
        bm25_top_k: int = 10,
    ):
        if not DEPENDENCIES_AVAILABLE:
            raise RuntimeError("RAG dependencies not installed: pymilvus, milvus-lite")

        self.knowledge_base_path = Path(knowledge_base_path)
        self.knowledge_base_path.mkdir(parents=True, exist_ok=True)
        self.collection_name = collection_name
        self.top_k = top_k
        self.vector_top_k = vector_top_k
        self.bm25_top_k = bm25_top_k

        model_name = resolve_embedding_model(embedding_model) if embedding_backend == "local" else embedding_model
        self.embedding_model = create_text_embedder(
            backend=embedding_backend,
            model=model_name,
            api_key=embedding_api_key,
            base_url=embedding_base_url,
            dimension=embedding_dimension,
            timeout_sec=embedding_timeout_sec,
            batch_size=embedding_batch_size,
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

        current_count = self.count()
        rows = []
        for offset, chunk in enumerate(chunks, start=1):
            doc_id = current_count + offset
            metadata = chunk.to_metadata()
            metadata["chunk_id"] = f"{Path(chunk.source_path).stem}_{chunk.chunk_index}"
            rows.append(
                {
                    "id": doc_id,
                    "content": chunk.content,
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                }
            )
        vectors = self.embedding_model.embed_texts([chunk.content for chunk in chunks])
        for row, vector in zip(rows, vectors):
            row["vector"] = vector

        self.client.insert(collection_name=self.collection_name, data=rows)
        self.load_collection()
        return {"status": "success", "added_count": len(rows), "total_count": self.count()}

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
        for doc_id, (chunk, vector) in enumerate(zip(chunks, vectors), start=1):
            metadata = chunk.to_metadata()
            metadata["chunk_id"] = f"{Path(chunk.source_path).stem}_{chunk.chunk_index}"
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
                if live_moved and self.client.has_collection(backup):
                    self.client.rename_collection(backup, self.collection_name)
                    live_moved = False
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
        for start in range(1, total + 1, chunk_size):
            end = min(start + chunk_size - 1, total)
            rows.extend(
                self.client.query(
                    collection_name=self.collection_name,
                    filter=f"id >= {start} and id <= {end}",
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
        docs = self.fetch_all_documents()
        if not docs:
            return []

        tokenized = [_tokenize(doc.get("content", "")) for doc in docs]
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        n_docs = len(tokenized)
        doc_lengths = [len(tokens) for tokens in tokenized]
        avgdl = sum(doc_lengths) / n_docs if n_docs else 0.0
        if avgdl <= 0:
            return []

        df: Dict[str, int] = {}
        for tokens in tokenized:
            for token in set(tokens):
                df[token] = df.get(token, 0) + 1

        k1 = 1.5
        b = 0.75
        scored = []
        for index, tokens in enumerate(tokenized):
            tf: Dict[str, int] = {}
            for token in tokens:
                tf[token] = tf.get(token, 0) + 1

            score = 0.0
            for query_token in query_tokens:
                if query_token not in tf:
                    continue
                freq = tf[query_token]
                doc_freq = df.get(query_token, 0)
                idf = math.log(1.0 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))
                denom = freq + k1 * (1 - b + b * len(tokens) / avgdl)
                score += idf * (freq * (k1 + 1) / denom)

            if score > 0:
                scored.append((index, score))

        scored.sort(key=lambda item: item[1], reverse=True)
        results = []
        for rank, (index, score) in enumerate(scored[: top_k or self.bm25_top_k], start=1):
            doc = docs[index]
            results.append({**doc, "bm25_score": score, "bm25_rank": rank})
        return results

    def hybrid_search(self, query: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        start = time.perf_counter()
        vector_docs = self.vector_search(query, self.vector_top_k)
        bm25_docs = self.bm25_search(query, self.bm25_top_k)
        final_k = top_k or self.top_k
        candidate_k = max(final_k, self.vector_top_k, self.bm25_top_k)
        fused_docs = fuse_results(vector_docs, bm25_docs, candidate_k)
        reranked_docs = rerank_results(fused_docs, query)
        filtered_docs = filter_relevant_results(reranked_docs, query)
        logger.info(
            "RAG hybrid search completed in %.3fs (vector=%d, bm25=%d, fused=%d, filtered=%d)",
            time.perf_counter() - start,
            len(vector_docs),
            len(bm25_docs),
            len(fused_docs),
            len(filtered_docs),
        )
        return (filtered_docs or reranked_docs)[:final_k]

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
    if not terms:
        return docs

    def score(doc: Dict[str, Any]) -> float:
        content = doc.get("content", "")
        matches = sum(1 for term in terms if term in content)
        title = str((doc.get("metadata") or {}).get("title", ""))
        title_matches = sum(1 for term in terms if term in title)
        penalty = _off_topic_penalty(query, content)
        return float(doc.get("fusion_score", 0.0)) + matches * 0.04 + title_matches * 0.02 - penalty

    for doc in docs:
        doc["rerank_score"] = score(doc)
    return sorted(docs, key=lambda doc: doc.get("rerank_score", 0.0), reverse=True)


def filter_relevant_results(docs: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    terms = _rerank_terms(query)
    if not terms:
        return docs

    return [
        doc
        for doc in docs
        if any(term in doc.get("content", "") for term in terms)
        or doc.get("vector_rank") == 1
        or doc.get("bm25_rank") == 1
    ]


def _rerank_terms(query: str) -> List[str]:
    terms = [term for term in ("餐补", "餐费", "餐饮", "早餐", "午餐", "晚餐", "报销", "个人零食", "酒水") if term in query]
    if any(term in query for term in ("餐补", "饭补", "吃饭")):
        terms.extend(["餐费", "餐饮", "早餐", "午餐", "晚餐", "报销"])
    return list(dict.fromkeys(terms))


def _off_topic_penalty(query: str, content: str) -> float:
    if not any(term in query for term in ("餐补", "餐费", "餐饮", "饭补", "吃饭")):
        return 0.0

    meal_terms = ("餐费", "餐饮", "早餐", "午餐", "晚餐", "业务招待", "个人零食", "饮料", "酒水")
    meal_matches = sum(1 for term in meal_terms if term in content)
    unrelated_terms = ("家属", "升级酒店", "升级机票", "国际出差", "签证", "护照", "里程", "积分")
    unrelated_matches = sum(1 for term in unrelated_terms if term in content)
    if meal_matches >= 2:
        return 0.0
    return unrelated_matches * 0.03


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
    zh_tokens = [char for char in text if "\u4e00" <= char <= "\u9fff"]
    return word_tokens + phrase_tokens + zh_tokens
