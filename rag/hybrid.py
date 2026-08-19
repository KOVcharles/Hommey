"""Backend-neutral hybrid retrieval over one active vector-store snapshot."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Protocol

from .ranking import filter_relevant_results, fuse_results, rerank_results
from .sparse import create_sparse_index
from .trace import append_retrieval_trace, build_retrieval_trace, new_trace_id

logger = logging.getLogger(__name__)


class HybridSearchBackend(Protocol):
    collection_name: str
    top_k: int
    vector_top_k: int
    bm25_top_k: int
    sparse_backend: str

    def vector_search(self, query: str, top_k: int) -> List[Dict[str, Any]]: ...
    def fetch_all_documents(self) -> List[Dict[str, Any]]: ...


def bm25_search(
    backend: HybridSearchBackend, query: str, top_k: int | None = None,
) -> List[Dict[str, Any]]:
    documents = backend.fetch_all_documents()
    if not documents:
        return []
    index = create_sparse_index(backend.sparse_backend)
    index.index(documents)
    return index.search(query, top_k or backend.bm25_top_k)


def hybrid_search(
    backend: HybridSearchBackend, query: str, top_k: int | None = None,
) -> List[Dict[str, Any]]:
    """Fuse dense and lexical candidates from the same active index version."""
    started = time.perf_counter()
    trace_id = new_trace_id()
    vector_docs = backend.vector_search(query, backend.vector_top_k)
    lexical_docs = bm25_search(backend, query, backend.bm25_top_k)
    final_k = top_k or backend.top_k
    candidate_k = max(final_k, backend.vector_top_k + backend.bm25_top_k)
    fused = fuse_results(vector_docs, lexical_docs, candidate_k)
    reranked = rerank_results(fused, query)
    filtered = filter_relevant_results(reranked, query)
    final = (filtered or reranked)[:final_k]
    for document in final:
        document["retrieval_trace_id"] = trace_id
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    append_retrieval_trace(
        build_retrieval_trace(
            trace_id=trace_id,
            query=query,
            expanded_query=query,
            top_k=final_k,
            docs=final,
            metrics={
                "candidates": len(fused),
                "kept": len(final),
                "dropped_by_filter": max(0, len(reranked) - len(filtered)) if filtered else 0,
                "reranked": len(reranked),
                "latency_ms": elapsed_ms,
            },
            index_version=str(
                ((final[0].get("metadata") or {}).get("index_version") if final else "") or ""
            ),
            collection_name=backend.collection_name,
        )
    )
    logger.info(
        "RAG hybrid search completed backend=%s latency_ms=%.2f vector=%d bm25=%d kept=%d",
        type(backend).__name__,
        elapsed_ms,
        len(vector_docs),
        len(lexical_docs),
        len(final),
    )
    return final
