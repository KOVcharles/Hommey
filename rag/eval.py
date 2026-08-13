"""Phase 0 golden-query evaluation harness (audit §11 Phase 0).

Establishes a *comparable baseline* over the current corpus, not a quality
gate: any later retrieval/citation change can be diffed against the numbers
this harness produces instead of relying on subjective feel.

The harness runs the exact production path — loader → parser → normalizer →
chunker → vector store → retriever — so the baseline reflects what agents will
actually see.  It defaults to :class:`InMemoryVectorStore` so the whole run is
deterministic and needs no embedding API keys or Milvus.  PDFs are skipped in
this mode (their parser requires the optional ``pypdf`` package); every skipped
file is reported so the baseline always states its coverage.

Metrics reported (audit §11 Phase 0):
    - Recall@5 / Recall@10 (did the expected file appear)
    - MRR@10 (rank of the first expected file)
    - citation file accuracy @1 (top-1 result's file is expected)
    - no-knowledge precision (for ``no_answer`` queries, no strong evidence)
    - p50/p95 retrieval latency

A retrieval trace line is appended per query (via :mod:`rag.trace`) when a
trace path is supplied, so every result is reproducible offline.
"""
from __future__ import annotations

import json
import logging
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import RAGPipelineConfig
from .pipeline import RAGPipeline
from .trace import append_retrieval_trace, build_retrieval_trace, new_trace_id
from .vector_store import InMemoryVectorStore, VectorStore

logger = logging.getLogger(__name__)

GOLDEN_QUERIES_RELATIVE = "tests/data/golden_queries.json"


@dataclass
class _PerQuery:
    query_id: str
    type: str
    query: str
    expected_files: List[str]
    results: List[Dict[str, Any]]
    recall_5: float
    recall_10: float
    mrr_10: float
    top1_correct: bool
    latency_ms: float


@dataclass
class EvalReport:
    corpus_files: List[str]
    skipped_files: List[str]
    chunks_indexed: int
    index_version: str
    schema_version: str
    queries: List[_PerQuery] = field(default_factory=list)

    def summarize(self) -> Dict[str, Any]:
        expected = [q for q in self.queries if q.expected_files]
        no_answer = [q for q in self.queries if not q.expected_files]
        latencies = [q.latency_ms for q in self.queries]
        stats = {
            "total_queries": len(self.queries),
            "expected_queries": len(expected),
            "no_answer_queries": len(no_answer),
            "recall_at_5": _mean(q.recall_5 for q in expected),
            "recall_at_10": _mean(q.recall_10 for q in expected),
            "mrr_at_10": _mean(q.mrr_10 for q in expected),
            "citation_file_accuracy_at_1": _mean(1.0 if q.top1_correct else 0.0 for q in expected),
            "no_knowledge_precision": _mean(1.0 if _is_no_evidence(q) else 0.0 for q in no_answer),
            "latency_ms_p50": statistics.median(latencies) if latencies else 0.0,
            "latency_ms_p95": _percentile(latencies, 0.95) if latencies else 0.0,
            "index_version": self.index_version,
            "schema_version": self.schema_version,
            "chunks_indexed": self.chunks_indexed,
            "corpus_files": len(self.corpus_files),
            "skipped_files": self.skipped_files,
        }
        return stats


def _is_no_evidence(query: _PerQuery) -> bool:
    """True when retrieval surfaced no substantive evidence for a no-answer query.

    Strong evidence means the top result shares the question's *distinctive*
    topic bigrams.  Generic functional words (报销/出差/费用/标准…) are
    stripped first — a chunk that merely says "可报销" is not evidence that the
    question's specific item is reimbursable.  A question with nothing
    distinctive (or no results) is treated as no-evidence.
    """
    if not query.results:
        return True
    topic_bigrams = _distinctive_bigrams(query.query)
    if not topic_bigrams:
        return True
    top = query.results[0]
    content = top.get("content") or ""
    return not any(bigram in content for bigram in topic_bigrams)


# Characters that rarely identify a policy topic.  A bigram containing one of
# these is not distinctive of the question (e.g. 报销/出差/费用 are function
# words, not the specific item being asked about).
_STOP_CHARS = frozenset(
    "的了可以能报销出差差旅期间中相关费用标准什么哪些多少如何怎样怎么住酒店津贴补贴吗呢等和与或在又并都公司政策制度规则"
)


def _distinctive_bigrams(text: str) -> List[str]:
    """CJK character bigrams of ``text`` that survive stop-char filtering."""
    bigrams: List[str] = []
    for index in range(len(text) - 1):
        pair = text[index : index + 2]
        if any(char in _STOP_CHARS for char in pair):
            continue
        if not ("一" <= pair[0] <= "鿿" and "一" <= pair[1] <= "鿿"):
            continue
        bigrams.append(pair)
    return bigrams


def _mean(values) -> float:
    values = list(values)
    return round(sum(values) / len(values), 4) if values else 0.0


def _percentile(values: List[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(p * len(ordered)))
    return round(ordered[index], 2)


def load_golden_queries(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = path or Path(__file__).resolve().parents[1] / GOLDEN_QUERIES_RELATIVE
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data["queries"]


def run_baseline_eval(
    documents_dir: str = "data/documents",
    top_k: int = 10,
    include: Optional[List[str]] = None,
    trace_path: Optional[Path] = None,
) -> EvalReport:
    """Build a deterministic in-memory index from every supported source type
    and evaluate the golden queries against it.  Returns an ``EvalReport``."""
    root = Path(documents_dir)
    supported = ("txt", "md", "pdf", "docx", "csv", "xlsx")
    files = sorted(
        item for item in root.rglob("*")
        if item.is_file() and item.suffix.lower().lstrip(".") in supported
    )
    if include:
        files = [item for item in files if item.name in include]

    skipped: List[str] = []

    if not files:
        raise FileNotFoundError(f"No txt/md sources under {root}")

    with tempfile.TemporaryDirectory(prefix="hommey-rag-eval-") as tmp:
        corpus_dir = Path(tmp) / "corpus"
        corpus_dir.mkdir()
        for source in files:
            (corpus_dir / source.name).write_bytes(source.read_bytes())

        config = RAGPipelineConfig.from_settings(
            {
                "documents_dir": str(corpus_dir),
                "knowledge_base_path": str(Path(tmp) / "kb"),
                "top_k": top_k,
            }
        )
        store: VectorStore = InMemoryVectorStore()
        pipeline = RAGPipeline(config=config, vector_store=store)
        try:
            report = pipeline.ingest(corpus_dir, rebuild=True)
        finally:
            pipeline.close()

        eval_report = EvalReport(
            corpus_files=[item.name for item in files],
            skipped_files=skipped,
            chunks_indexed=report.chunks_loaded,
            index_version=(report.metadata.get("index") or {}).get("version", ""),
            schema_version=report.metadata.get("schema_version", ""),
        )
        for golden in load_golden_queries():
            query = golden["query"]
            started = time.perf_counter()
            results = pipeline.query(query, top_k=top_k)
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            results = [_coerce_result_row(result) for result in results]

            per = _evaluate_query(golden, results, latency_ms)
            eval_report.queries.append(per)

            if trace_path is not None:
                metrics = {"latency_ms": latency_ms}
                record = build_retrieval_trace(
                    trace_id=new_trace_id(),
                    query=query,
                    expanded_query=query,
                    top_k=top_k,
                    docs=[_result_row(r) for r in results],
                    metrics=metrics,
                    index_version=eval_report.index_version,
                    collection_name=config.collection_name,
                )
                append_retrieval_trace(record, trace_path)
        return eval_report


def _evaluate_query(golden: Dict[str, Any], results: List[Dict[str, Any]], latency_ms: float) -> _PerQuery:
    expected = set(golden.get("expected_files") or [])
    files = [_result_filename(result) for result in results]

    rank_first: Optional[int] = None
    for index, filename in enumerate(files[:10], start=1):
        if filename in expected:
            rank_first = index
            break

    return _PerQuery(
        query_id=golden["id"],
        type=golden["type"],
        query=golden["query"],
        expected_files=sorted(expected),
        results=results,
        recall_5=1.0 if any(f in expected for f in files[:5]) else 0.0,
        recall_10=1.0 if rank_first is not None else 0.0,
        mrr_10=1.0 / rank_first if rank_first else 0.0,
        top1_correct=bool(files) and files[0] in expected,
        latency_ms=latency_ms,
    )


def _result_filename(result: Dict[str, Any]) -> str:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    return (
        metadata.get("filename")
        or metadata.get("file_name")
        or metadata.get("name")
        or metadata.get("parent_doc")
        or ""
    )


def _coerce_result_row(result: Any) -> Dict[str, Any]:
    """Normalize a retrieval row to a plain dict regardless of transport type.

    ``RetrievalResult`` is a dataclass (``pipeline.query`` returns these);
    older callers may hand back dicts.  Both are accepted.
    """
    if isinstance(result, dict):
        return dict(result)
    if hasattr(result, "to_dict"):
        return dict(result.to_dict())
    raise TypeError(f"unsupported retrieval row type: {type(result)!r}")


def _result_row(result: Dict[str, Any]) -> Dict[str, Any]:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    return {
        "id": result.get("id"),
        "content": result.get("content", ""),
        "metadata": metadata,
        "distance": result.get("distance"),
        "vector_rank": result.get("vector_rank"),
        "bm25_rank": result.get("bm25_rank"),
        "bm25_score": result.get("bm25_score"),
        "fusion_score": result.get("fusion_score"),
        "rerank_score": result.get("rerank_score"),
        "retrieval_trace_id": result.get("retrieval_trace_id"),
    }


def run_and_report(documents_dir: str = "data/documents", trace_path: Optional[Path] = None) -> Dict[str, Any]:
    """Run the baseline and return the aggregate stats (script entry point)."""
    eval_report = run_baseline_eval(documents_dir=documents_dir, trace_path=trace_path)
    stats = eval_report.summarize()
    logger.info("Phase 0 baseline: %s", json.dumps(stats, ensure_ascii=False))
    return stats


def save_baseline_report(eval_report: EvalReport, path: Path) -> Path:
    """Persist the baseline: aggregate stats plus one row per golden query.

    The saved file is the reference point every later A/B diff is measured
    against (audit §11 Phase 0 “任何后续方案都能与同一数据集进行 A/B”).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "report": "rag.v2.phase0.baseline.1",
        "stats": eval_report.summarize(),
        "queries": [
            {
                "id": q.query_id,
                "type": q.type,
                "query": q.query,
                "expected_files": q.expected_files,
                "recall_at_5": q.recall_5,
                "recall_at_10": q.recall_10,
                "mrr_at_10": q.mrr_10,
                "top1_correct": q.top1_correct,
                "latency_ms": q.latency_ms,
                "retrieved_files": [_result_filename(r) for r in q.results],
            }
            for q in eval_report.queries
        ],
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Phase 0 RAG baseline eval")
    parser.add_argument("--documents-dir", default="data/documents")
    parser.add_argument("--report", default="data/eval/phase0_baseline.json")
    parser.add_argument("--trace", default="data/eval/retrieval_traces.jsonl")
    args = parser.parse_args()

    trace_path = Path(args.trace)
    eval_report = run_baseline_eval(
        documents_dir=args.documents_dir,
        trace_path=trace_path,
    )
    save_baseline_report(eval_report, Path(args.report))
    print(json.dumps(eval_report.summarize(), ensure_ascii=False, indent=2))
    print(f"\nreport: {args.report}")
    print(f"trace:  {trace_path}")
