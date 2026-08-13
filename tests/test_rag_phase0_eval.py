"""Phase 0 baseline eval harness tests (audit §11 Phase 0).

These tests keep the *baseline harness* alive and assert it reports every
metric Phase 0 demands — they do not assert a quality bar that Phase 4 will
eventually move.  The numbers are a floor for the harness itself, so a broken
chunker/retriever surfaces immediately instead of silently changing the
baseline.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from rag import schemas
from rag.eval import (
    GOLDEN_QUERIES_RELATIVE,
    _is_no_evidence,
    load_golden_queries,
    run_baseline_eval,
)


def test_golden_query_set_covers_all_six_categories():
    queries = load_golden_queries(Path(__file__).parent / "data" / "golden_queries.json")
    categories = {query["type"] for query in queries}
    assert {"exact", "colloquial", "negation", "city_scope", "multi_fact", "no_answer"} <= categories
    assert len(queries) >= 30


def test_golden_queries_declare_expected_files_for_answerable_questions():
    queries = load_golden_queries(Path(__file__).parent / "data" / "golden_queries.json")
    for query in queries:
        if query["type"] == "no_answer":
            assert query.get("expected_files") == []
        else:
            assert query.get("expected_files"), f"{query['id']} must name expected files"


def test_baseline_eval_reports_all_phase0_metrics():
    report = run_baseline_eval()
    stats = report.summarize()

    for metric in (
        "recall_at_5",
        "recall_at_10",
        "mrr_at_10",
        "citation_file_accuracy_at_1",
        "no_knowledge_precision",
        "latency_ms_p50",
        "latency_ms_p95",
    ):
        assert metric in stats, f"missing metric {metric}"
        assert isinstance(stats[metric], (int, float))

    assert stats["index_version"]
    assert stats["schema_version"] == schemas.SCHEMA_VERSION
    assert stats["chunks_indexed"] > 0
    assert report.corpus_files, "eval corpus must be non-empty"


def test_baseline_covers_supported_pdf_sources():
    report = run_baseline_eval()
    available_pdfs = {path.name for path in Path("data/documents").glob("*.pdf")}

    assert available_pdfs <= set(report.corpus_files)
    assert not (available_pdfs & set(report.skipped_files))


def test_baseline_recall_floor_is_sane():
    # The golden queries are written so token-match retrieval recovers the
    # expected file almost always at top-10; a broken chunker would crater this.
    stats = run_baseline_eval().summarize()
    assert stats["recall_at_10"] >= 0.8
    assert stats["recall_at_5"] >= 0.7


def test_no_answer_queries_get_no_strong_evidence():
    # Phase 0 expects retrieval not to fabricate strong evidence for
    # unanswerable questions; the evidence gate that *uses* this is Phase 4.
    report = run_baseline_eval()
    no_answer = [query for query in report.queries if not query.expected_files]
    assert no_answer, "golden set must contain no_answer queries"
    assert all(_is_no_evidence(query) for query in no_answer)


def test_eval_can_emit_retrieval_traces(tmp_path: Path):
    trace_path = tmp_path / "traces.jsonl"
    report = run_baseline_eval(trace_path=trace_path)
    assert report.queries
    lines = [line for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == len(report.queries)
    import json

    first = json.loads(lines[0])
    assert first["schema_version"].startswith("rag.v2.trace.")
    assert first["results"]
