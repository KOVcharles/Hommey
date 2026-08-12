"""Retrieval trace emission (audit §6.1.5 三, Phase 0).

The trace is the reproducible record of a single query: which branch scores
fed the fusion, what the reranker kept, what the domain filter dropped, and
which chunks finally entered the answer.  Traces append to one JSONL file so
every A/B candidate can be diffed against the Phase 0 baseline.

The store layer is only responsible for minting ``retrieval_trace_id`` and
attaching it to result rows; this module owns the payload shape and the file.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

TRACE_SCHEMA_VERSION = "rag.v2.trace.1"
DEFAULT_TRACE_RELATIVE = "data/rag_knowledge/retrieval_traces.jsonl"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_trace_id() -> str:
    """Mint a compact, globally unique trace id."""
    return uuid.uuid4().hex[:16]


def default_trace_path() -> Path:
    """Resolve the append-only trace file relative to the project root."""
    env = os.environ.get("HOMMEY_RAG_TRACE_FILE")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[1] / DEFAULT_TRACE_RELATIVE


def build_retrieval_trace(
    *,
    trace_id: str,
    query: str,
    expanded_query: str,
    top_k: int,
    docs: List[Dict[str, Any]],
    metrics: Dict[str, Any],
    index_version: str = "",
    collection_name: str = "",
    answer: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble one trace record from the ranked results.

    ``docs`` are the final result rows (already carrying rerank_score /
    retrieval_trace_id).  Each entry keeps only the branch scores plus the
    citation projection needed to revalidate the answer offline.
    """
    results = []
    for rank, doc in enumerate(docs, start=1):
        metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        results.append(
            {
                "chunk_id": metadata.get("chunk_id") or doc.get("chunk_id"),
                "chunk_hash": metadata.get("chunk_hash") or doc.get("chunk_hash"),
                "document_id": metadata.get("document_id") or doc.get("document_id"),
                "heading_path": metadata.get("heading_path") or [],
                "content_type": metadata.get("content_type") or "",
                "page_start": metadata.get("page_start") or metadata.get("page_number"),
                "vector_rank": doc.get("vector_rank"),
                "distance": doc.get("distance"),
                "bm25_rank": doc.get("bm25_rank"),
                "bm25_score": doc.get("bm25_score"),
                "fusion_score": doc.get("fusion_score"),
                "rerank_score": doc.get("rerank_score"),
                "final_rank": rank,
            }
        )
    record: Dict[str, Any] = {
        "trace_id": trace_id,
        "schema_version": TRACE_SCHEMA_VERSION,
        "created_at": _utc_now_iso(),
        "index_version": index_version,
        "collection_name": collection_name,
        "query": {
            "question": query,
            "expanded_query": expanded_query,
            "top_k": top_k,
        },
        "metrics": metrics,
        "results": results,
    }
    if answer:
        record["answer"] = answer
    return record


def append_retrieval_trace(record: Dict[str, Any], trace_path: Optional[Path] = None) -> Optional[Path]:
    """Append one trace line (best-effort; never raises on I/O failure).

    Returns the path written, or None when emission was skipped.
    """
    if not isinstance(record, dict) or not record.get("trace_id"):
        return None
    path = trace_path or default_trace_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path
    except OSError as exc:
        logger.warning("Failed to append retrieval trace %s: %s", path, exc)
        return None


def read_traces(trace_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read back all trace records (for offline A/B analysis)."""
    path = trace_path or default_trace_path()
    records: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return records
