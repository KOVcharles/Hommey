"""Phase 4 regression tests (audit §11 Phase 4: 通用 evidence gate 与状态机).

Scope delivered: a deterministic, scale-free evidence classifier
(``rag.evidence.evaluate_evidence``) gates every query before the answer is
generated, and the ask-question agent maps its verdict onto the status machine:
``sufficient`` → ``success``, ``partial`` → hedged ``partial`` answer,
``insufficient`` → ``no_knowledge``.

- 空/零覆盖 → insufficient       → ``test_evidence_empty_docs_insufficient``
- 高覆盖率 → sufficient          → ``test_evidence_high_coverage_sufficient``
- rerank 提升挽救中覆盖率         → ``test_evidence_lift_rescues_mid_coverage``
- 中覆盖率无提升 → partial        → ``test_evidence_mid_coverage_no_lift_partial``
- 高 rerank 低覆盖仍 partial      → ``test_evidence_high_rerank_low_coverage_partial``
- 按 rerank 排序取 top-N          → ``test_evidence_uses_rerank_ordered_top_n``
- 超短查询走 lift 判定            → ``test_evidence_degenerate_short_query_uses_lift``
- 2-gram tokenizer 修复           → ``test_evidence_cjk_bigrams_cover_short_queries``
- 状态机接线（agent reply）       → ``test_reply_*``
- golden 语料不变量               → ``test_golden_corpus_invariants``
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest
from agentscope.message import Msg

from rag.evidence import EvidenceVerdict, evaluate_evidence
from rag.milvus_store import _focus_terms, _query_ngrams, rerank_results


def _doc(content: str, *, fusion: float = 0.5, rerank: float = 0.5, **metadata) -> dict:
    return {
        "id": metadata.get("_id", 1),
        "content": content,
        "metadata": {k: v for k, v in metadata.items() if not k.startswith("_")},
        "fusion_score": fusion,
        "rerank_score": rerank,
    }


# ---- evidence classifier unit cases ---------------------------------------


def test_evidence_empty_docs_insufficient():
    verdict = evaluate_evidence("火星出差报销标准", [])
    assert verdict.verdict == "insufficient"
    assert verdict.to_dict()["verdict"] == "insufficient"
    assert verdict.reasons


def test_evidence_zero_coverage_insufficient():
    verdict = evaluate_evidence(
        "火星出差报销标准",
        [_doc("海滩 美食 旅游攻略 景点", fusion=1.0, rerank=1.0)],
    )
    assert verdict.verdict == "insufficient"
    assert verdict.coverage == 0.0


def test_evidence_high_coverage_sufficient():
    verdict = evaluate_evidence(
        "住宿标准",
        [_doc("一线城市住宿标准为每晚600元")],
    )
    assert verdict.verdict == "sufficient"
    assert verdict.coverage >= 0.4


def test_evidence_lift_rescues_mid_coverage():
    # coverage 0.30 (in [0.25, 0.40)) is rescued by a rerank lift >= 0.15.
    verdict = evaluate_evidence(
        "出差期间加班有没有补贴",
        [_doc("出差加班补贴：工作日加班每人每天补贴30元", fusion=0.5, rerank=0.65)],
    )
    assert verdict.coverage == pytest.approx(0.30)
    assert verdict.rerank_lift >= 0.15
    assert verdict.verdict == "sufficient"


def test_evidence_mid_coverage_no_lift_partial():
    # Same query/evidence as above but without the rerank boost → partial.
    verdict = evaluate_evidence(
        "出差期间加班有没有补贴",
        [_doc("出差加班补贴：工作日加班每人每天补贴30元", fusion=0.5, rerank=0.5)],
    )
    assert verdict.verdict == "partial"


def test_evidence_high_rerank_low_coverage_partial():
    """The no-answer guard: a high rerank lift must NOT grant sufficient when
    the evidence barely overlaps the query.  Generic policy terms like 报销
    inflate rerank; coverage keeps the gate honest."""
    verdict = evaluate_evidence(
        "出差时酒店泳池使用费能报销吗",
        [_doc("出差期间住宿费实报实销，酒店费用按标准执行", fusion=0.5, rerank=0.95)],
    )
    assert verdict.verdict == "partial"
    assert verdict.coverage < 0.25
    assert verdict.rerank_lift >= 0.4  # strong rerank, still not sufficient


def test_evidence_uses_rerank_ordered_top_n():
    query = "住宿标准"
    a = _doc("机票预订提前三天", fusion=0.5, rerank=0.5, _id="a")
    b = _doc("餐费标准每人每天100元", fusion=0.5, rerank=0.5, _id="b")
    c = _doc("交通报销规定", fusion=0.5, rerank=0.5, _id="c")
    d = _doc("一线城市住宿标准为每晚600元", fusion=0.5, rerank=0.3, _id="d")
    docs = [a, b, c, d]

    # Default top_n=3 keeps d (ranked 4th) out: only 标准 matches → partial.
    three = evaluate_evidence(query, docs)
    assert three.verdict == "partial"
    assert three.coverage == pytest.approx(1 / 3)

    # top_n=4 includes d → full coverage → sufficient.
    four = evaluate_evidence(query, docs, top_n=4)
    assert four.verdict == "sufficient"
    assert four.coverage == pytest.approx(1.0)


def test_evidence_degenerate_short_query_uses_lift():
    # Single-token query ("报销") cannot be judged by coverage; falls back to lift.
    with_lift = evaluate_evidence("报销", [_doc("餐费报销", fusion=0.5, rerank=0.7)])
    assert with_lift.verdict == "sufficient"

    no_lift = evaluate_evidence("报销", [_doc("餐费报销", fusion=0.5, rerank=0.5)])
    assert no_lift.verdict == "partial"


def test_evidence_cjk_bigrams_cover_short_queries():
    """Regression: the rerank pipeline's 3–4 gram ngrams leave short policy
    queries with zero coverage ("报销流程" → cov 0.00).  The evidence gate uses
    CJK 2-grams so short queries get a real coverage signal."""
    content = "报销申请流程：提交发票后五个工作日内完成"
    verdict = evaluate_evidence("报销流程", [_doc(content)])
    assert verdict.verdict == "sufficient"
    assert verdict.coverage >= 0.5
    # Prove the 3–4 gram tokenizer alone would have missed it.
    assert not any(gram in content for gram in _query_ngrams("报销流程"))


def test_lodging_rerank_keeps_city_specific_standard_ahead_of_generic_fees():
    query = "北京出差住宿费标准是多少"
    docs = [
        _doc("报销金额计算 住宿费按实际天数计算", fusion=0.031, _id="generic"),
        _doc("住宿标准 一线城市北京不超过500元/晚", fusion=0.032, _id="beijing"),
        _doc("国际住宿上限为1600元", fusion=0.030, _id="international"),
    ]

    ranked = rerank_results(docs, query)

    assert _focus_terms(query) == ["北京", "住宿费"]
    assert ranked[0]["id"] == "beijing"


def test_evidence_to_dict_shape():
    verdict = evaluate_evidence("住宿标准", [_doc("一线城市住宿标准")])
    data = verdict.to_dict()
    assert set(data) == {"verdict", "score", "coverage", "rerank_lift",
                         "matched_terms", "total_terms", "reasons"}


def test_evidence_verdict_is_frozen_dataclass():
    verdict = evaluate_evidence("住宿标准", [_doc("一线城市住宿标准")])
    assert isinstance(verdict, EvidenceVerdict)
    with pytest.raises(Exception):
        verdict.verdict = "partial"  # type: ignore[misc]


# ---- ask-question agent status-machine wiring -----------------------------


def _load_agent_module(name: str):
    script_path = Path(".agents/skills/ask-question/script/agent.py")
    spec = importlib.util.spec_from_file_location(name, script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_agent(module, docs=None):
    # object.__new__ skips AgentBase.__init__, which normally seeds the instance
    # hook registries the metaclass checks before calling reply().  Mirror just
    # those attrs (empty → no hooks) instead of running the real __init__, which
    # would build a KnowledgeRetriever.
    from collections import OrderedDict

    agent = object.__new__(module.RAGKnowledgeAgent)
    agent.initialized = True
    agent.name = "RAGKnowledgeAgent"
    agent.model = None
    agent._instance_pre_reply_hooks = OrderedDict()
    agent._instance_post_reply_hooks = OrderedDict()
    agent._instance_pre_print_hooks = OrderedDict()
    agent._instance_post_print_hooks = OrderedDict()
    agent._retrieve_for_question = lambda q: docs or []
    agent.get_stats = lambda: {"status": "success", "total_documents": 3}
    return agent


def _reply(module, agent, query: str):
    msg = asyncio.run(
        agent.reply(Msg(name="Orchestrator", role="user", content=query))
    )
    return json.loads(msg.content)


def test_reply_insufficient_evidence_returns_no_knowledge():
    module = _load_agent_module("rag_agent_test_ev_insufficient")
    agent = _make_agent(module, docs=[
        _doc("海滩 美食 旅游攻略 景点", fusion=1.0, rerank=1.0),
    ])
    data = _reply(module, agent, "火星出差报销标准")
    assert data["status"] == "no_knowledge"
    assert data["retrieved_documents"] == []
    assert data["evidence"]["verdict"] == "insufficient"


def test_reply_sufficient_evidence_returns_success():
    module = _load_agent_module("rag_agent_test_ev_sufficient")
    agent = _make_agent(module, docs=[
        _doc("一线城市住宿标准为每晚600元", filename="01_travel_standards.txt",
             chunk_id="c1"),
    ])
    data = _reply(module, agent, "住宿标准")
    assert data["status"] == "success"
    assert data["evidence"]["verdict"] == "sufficient"
    assert "知识库中的相关信息" in data["answer"]
    assert data["sources"][0]["file"] == "01_travel_standards.txt"


def test_reply_partial_evidence_returns_partial_status():
    module = _load_agent_module("rag_agent_test_ev_partial")
    agent = _make_agent(module, docs=[
        _doc("出差期间住宿费实报实销，酒店费用按标准执行", fusion=0.5, rerank=0.95,
             filename="01_travel_standards.txt", chunk_id="c1"),
    ])
    data = _reply(module, agent, "出差时酒店泳池使用费能报销吗")
    assert data["status"] == "partial"
    assert data["evidence"]["verdict"] == "partial"
    assert "知识库中的相关信息" in data["answer"]


def test_reply_empty_knowledge_base_unchanged():
    module = _load_agent_module("rag_agent_test_ev_empty_kb")
    agent = _make_agent(module, docs=[])
    agent.get_stats = lambda: {"status": "success", "total_documents": 0}
    data = _reply(module, agent, "住宿标准")
    assert data["status"] == "knowledge_base_empty"


def test_reply_no_query_returns_no_knowledge():
    module = _load_agent_module("rag_agent_test_ev_no_query")
    agent = _make_agent(module, docs=[_doc("一线城市住宿标准为每晚600元")])
    data = _reply(module, agent, "")
    assert data["status"] == "no_knowledge"


def test_reply_initialization_error_satisfies_runtime_schema():
    module = _load_agent_module("rag_agent_test_ev_init_error")
    agent = _make_agent(module)
    agent.initialized = False
    agent.retriever = type("Retriever", (), {"error": "not configured"})()

    data = _reply(module, agent, "住宿标准")

    assert data["status"] == "error"
    assert data["answer"]
    module._OUTPUT_VALIDATOR.validate(data)


# ---- golden corpus invariant -----------------------------------------------


GOLDEN_PATH = Path("tests/data/golden_queries.json")
CORPUS_DIR = Path("data/documents")


@pytest.mark.skipif(
    not GOLDEN_PATH.exists() or not CORPUS_DIR.is_dir(),
    reason="golden queries or corpus not available in this checkout",
)
def test_golden_corpus_invariants():
    """Locks the calibration: on the real corpus, no no-answer query may be
    judged sufficient (hallucination risk), and no answer-bearing query may be
    judged insufficient (false no-knowledge).  The hedged middle absorbs the
    rest."""
    from rag.chunker import BlockChunker
    from rag.parser import ParserRegistry
    from rag.schemas import RawDocument
    from rag.vector_store import InMemoryVectorStore

    registry = ParserRegistry()
    chunks = []
    for path in sorted(CORPUS_DIR.glob("*.txt")):
        raw = RawDocument(
            content=path.read_bytes(),
            source_path=str(path),
            filename=path.name,
            file_type="txt",
            metadata={"document_version": "v1"},
        )
        chunks.extend(BlockChunker(max_tokens=400).chunk(registry.parse(raw)))
    store = InMemoryVectorStore()
    store.add_chunks(chunks)

    golden = json.loads(GOLDEN_PATH.read_text())
    violations = []
    for query in golden["queries"]:
        results = [r.to_dict() for r in store.search(query["query"], top_k=6)]
        verdict = evaluate_evidence(query["query"], results)
        intent = query["type"]
        if intent == "no_answer" and verdict.verdict == "sufficient":
            violations.append(f"no_answer judged sufficient: {query['query']}")
        if intent != "no_answer" and verdict.verdict == "insufficient":
            violations.append(f"answer-bearing judged insufficient: {query['query']}")

    assert not violations, "golden corpus invariants violated:\n" + "\n".join(violations)
