import asyncio
import importlib.util
import json

from rag.hyde import merge_enhanced_results, validate_hyde_output
from webui_new.schemas.requests import ChatRequest


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
