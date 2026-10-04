from pathlib import Path
from types import SimpleNamespace

import pytest

from rag.embedder import SiliconFlowEmbedder
from rag.ranking import _tokenize, fuse_results, rerank_results
from rag.retriever import expand_query
from rag.vector_store import InMemoryVectorStore, create_vector_store


def test_tokenize_uses_domain_words_synonyms_and_stopwords():
    tokens = _tokenize("请问帮我查一下酒店房费、饭补和高铁票相关报销")

    assert {"住宿", "住宿费", "餐补", "火车票", "报销"} <= set(tokens)
    assert not {"请问", "帮我", "查一下", "相关", "酒店", "房费", "饭补", "高铁票"} & set(tokens)
    assert _tokenize("住宿标准与发票") == ["住宿标准", "发票"]


def test_vector_store_factory_has_one_production_backend_and_memory_for_tests():
    assert isinstance(
        create_vector_store(SimpleNamespace(vector_backend="memory")),
        InMemoryVectorStore,
    )
    with pytest.raises(ValueError, match="Use postgres or memory"):
        create_vector_store(SimpleNamespace(vector_backend="legacy_local"))


def test_fuse_results_prefers_docs_seen_by_both_retrievers():
    vector_docs = [
        {"id": 1, "content": "北京住宿标准", "metadata": {}, "distance": 0.9},
        {"id": 2, "content": "成都交通建议", "metadata": {}, "distance": 0.8},
    ]
    bm25_docs = [
        {"id": 1, "content": "北京住宿标准", "metadata": {}, "bm25_score": 3.0},
        {"id": 3, "content": "上海报销要求", "metadata": {}, "bm25_score": 2.0},
    ]

    results = fuse_results(vector_docs, bm25_docs, top_k=3)

    assert results[0]["id"] == 1
    assert results[0]["vector_rank"] == 1
    assert results[0]["bm25_rank"] == 1


def test_meal_allowance_query_expands_and_reranks_meal_policy():
    query = expand_query("我出差有餐补吗")
    docs = [
        {"id": 1, "content": "国际出差有每日补贴，标准因国家而异", "metadata": {}, "fusion_score": 0.04},
        {"id": 2, "content": "午餐和晚餐可报销，每餐不超过100元；个人零食、酒水不予报销", "metadata": {}, "fusion_score": 0.02},
    ]

    results = rerank_results(docs, query)

    assert "餐费" in query
    assert results[0]["id"] == 2


def test_rerank_rewards_exact_chinese_concept_over_generic_expense_text():
    docs = [
        {"id": 1, "content": "出差期间其他合理费用可以凭发票报销", "metadata": {}, "fusion_score": 0.04},
        {"id": 2, "content": "宠物寄养费是否可报销没有政策授权，需要人工确认", "metadata": {}, "fusion_score": 0.015},
    ]

    results = rerank_results(docs, "出差期间宠物寄养费可以报销吗")

    assert results[0]["id"] == 2


def test_international_meal_query_expands_with_international_context():
    query = expand_query("去新加坡出差，公司提供午餐时餐补怎么扣")

    assert "国际出差" in query
    assert "境外" in query


def test_siliconflow_embedder_posts_openai_compatible_payload():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data": [
                    {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                    {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                ]
            }

    class FakeSession:
        def __init__(self):
            self.calls = []

        def post(self, url, headers, json, timeout):
            self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return FakeResponse()

    session = FakeSession()
    embedder = SiliconFlowEmbedder(
        api_key="test-key",
        model="BAAI/bge-m3",
        base_url="https://api.siliconflow.cn/v1/",
        dimension=3,
        timeout_sec=12,
        batch_size=8,
        session=session,
    )

    embeddings = embedder.embed_texts(["hello", "world"])

    assert embeddings == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    assert session.calls[0]["url"] == "https://api.siliconflow.cn/v1/embeddings"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer test-key"
    assert session.calls[0]["json"] == {
        "model": "BAAI/bge-m3",
        "input": ["hello", "world"],
        "encoding_format": "float",
    }
    assert session.calls[0]["timeout"] == 12
