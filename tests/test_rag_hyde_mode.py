import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from rag.hyde import merge_enhanced_results, validate_hyde_output
from webui_new.schemas.requests import ChatRequest


def _load_agent_module():
    path = Path(".agents/skills/ask-question/script/agent.py")
    spec = importlib.util.spec_from_file_location("rag_agent_hyde_test_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Retriever:
    top_k = 3

    def __init__(self):
        self.standard_queries = []
        self.dense_queries = []

    def search(self, query, top_k=None):
        self.standard_queries.append((query, top_k))
        return [{
            "id": "standard",
            "chunk_id": "chunk-standard",
            "content": "遗失发票需要提交情况说明",
            "metadata": {"chunk_id": "chunk-standard"},
        }]

    def search_dense(self, query, top_k=None):
        self.dense_queries.append((query, top_k))
        return [{
            "id": "hyde",
            "chunk_id": "chunk-hyde",
            "content": "票据遗失处理和补充材料要求",
            "metadata": {"chunk_id": "chunk-hyde"},
        }]


def _agent(module, model):
    agent = object.__new__(module.RAGKnowledgeAgent)
    agent.retriever = _Retriever()
    agent.model = model
    return agent


def test_chat_request_defaults_to_standard_and_accepts_enhanced():
    assert ChatRequest(message="标准").retrieval_mode == "standard"
    assert ChatRequest(message="增强", retrieval_mode="enhanced").retrieval_mode == "enhanced"


def test_hyde_validation_rejects_invented_hard_facts():
    safe = validate_hyde_output(
        "发票丢了怎么报销",
        "员工发生票据遗失时，应查询公司制度中的遗失票据处理、补充证明材料和审批流程相关条款。",
    )
    unsafe = validate_hyde_output(
        "发票丢了怎么报销",
        "员工发生票据遗失时可以报销，并应在提交情况说明后按五百元标准办理。",
    )

    assert safe.valid is True
    assert unsafe.valid is False
    assert unsafe.reason == "invented_policy_conclusion"


def test_weighted_rrf_keeps_only_real_deduplicated_chunks():
    standard = [{"chunk_id": "a", "content": "A"}, {"chunk_id": "b", "content": "B"}]
    hyde = [{"chunk_id": "b", "content": "B"}, {"chunk_id": "c", "content": "C"}]

    merged = merge_enhanced_results(standard, hyde, top_k=10, hyde_weight=0.6)

    assert [item["chunk_id"] for item in merged] == ["b", "a", "c"]
    assert all("enhanced_fusion_score" in item for item in merged)


def test_standard_mode_never_calls_hyde_model_or_dense_search():
    module = _load_agent_module()

    class ForbiddenModel:
        async def __call__(self, _messages):
            raise AssertionError("standard retrieval must not call HyDE")

    agent = _agent(module, ForbiddenModel())
    docs, diagnostics = asyncio.run(
        agent._retrieve_for_question("发票丢了怎么报销", retrieval_mode="standard")
    )

    assert docs[0]["chunk_id"] == "chunk-standard"
    assert diagnostics is None
    assert agent.retriever.dense_queries == []


def test_enhanced_mode_calls_dense_only_branch_and_records_trace(tmp_path, monkeypatch):
    module = _load_agent_module()
    trace_path = tmp_path / "hyde-traces.jsonl"
    monkeypatch.setitem(module.RAG_CONFIG, "hyde_enabled", True)
    monkeypatch.setitem(module.RAG_CONFIG, "hyde_trace_file", str(trace_path))
    monkeypatch.setitem(module.RAG_CONFIG, "hyde_candidate_top_k", 7)

    class HyDEModel:
        async def __call__(self, _messages):
            return {
                "content": "员工发生票据遗失时，应检索遗失票据处理、补充证明材料、审批流程和费用凭证要求等公司制度条款。"
            }

    agent = _agent(module, HyDEModel())
    docs, diagnostics = asyncio.run(
        agent._retrieve_for_question(
            "发票丢了怎么报销",
            retrieval_mode="enhanced",
            request_id="request-hyde-1",
        )
    )

    assert diagnostics.status == "enhanced"
    assert diagnostics.effective_mode == "enhanced"
    assert agent.retriever.dense_queries[0][1] == 7
    assert {doc["chunk_id"] for doc in docs} == {"chunk-standard", "chunk-hyde"}
    trace = json.loads(trace_path.read_text(encoding="utf-8").strip())
    assert trace["request_id"] == "request-hyde-1"
    assert trace["synthetic"] is True
    assert trace["query_hash"] and trace["output_hash"]
    assert set(trace["selected_chunk_ids"]) == {"chunk-standard", "chunk-hyde"}
    assert "发票丢了" not in trace_path.read_text(encoding="utf-8")


def test_invalid_hyde_output_falls_back_without_dense_search(tmp_path, monkeypatch):
    module = _load_agent_module()
    trace_path = tmp_path / "hyde-fallback.jsonl"
    monkeypatch.setitem(module.RAG_CONFIG, "hyde_enabled", True)
    monkeypatch.setitem(module.RAG_CONFIG, "hyde_trace_file", str(trace_path))

    class UnsafeModel:
        async def __call__(self, _messages):
            return {"content": "员工遗失发票后可以报销，并按500元标准提交给财务负责人审批处理。"}

    agent = _agent(module, UnsafeModel())
    docs, diagnostics = asyncio.run(
        agent._retrieve_for_question(
            "发票丢了怎么报销",
            retrieval_mode="enhanced",
            request_id="request-hyde-fallback",
        )
    )

    assert [doc["chunk_id"] for doc in docs] == ["chunk-standard"]
    assert diagnostics.status == "fallback"
    assert diagnostics.effective_mode == "standard"
    assert diagnostics.fallback_reason in {
        "invented_numeric_fact", "invented_policy_scope", "invented_policy_conclusion"
    }
    assert agent.retriever.dense_queries == []
    trace = json.loads(trace_path.read_text(encoding="utf-8").strip())
    assert trace["status"] == "fallback"
    assert trace["fallback_reason"] == diagnostics.fallback_reason


def test_manager_projects_hyde_status_to_answer_document():
    from webui_new.manager import HommeyWebInstance

    pipeline = SimpleNamespace(results=[SimpleNamespace(
        agent_name="rag_knowledge",
        task_id="rag-task",
        data={
            "retrieval": {
                "requested_mode": "enhanced",
                "effective_mode": "standard",
                "status": "fallback",
                "fallback_reason": "generation_timeout",
            }
        },
    )])

    presentation = HommeyWebInstance._retrieval_presentation(pipeline)

    assert presentation.requested_mode == "enhanced"
    assert presentation.effective_mode == "standard"
    assert presentation.status == "fallback"
