from pathlib import Path
from types import SimpleNamespace

import pytest

from rag.chunker import split_text
from rag.document_loader import load_text_documents
from rag.embedder import SiliconFlowEmbedder
from rag.ranking import _tokenize, fuse_results, rerank_results
from rag.retriever import expand_query
from rag.vector_store import InMemoryVectorStore, create_vector_store


def test_load_text_documents_reads_txt_files(tmp_path: Path):
    doc = tmp_path / "01_travel_standards.txt"
    doc.write_text("Travel Standards\n\n北京住宿标准。", encoding="utf-8")

    documents = load_text_documents(str(tmp_path))

    assert len(documents) == 1
    assert documents[0].title == "Travel Standards"
    assert documents[0].category == "travel_policy"
    # Citation identity comes from the filename/canonical fields; the legacy
    # `parent_doc` metadata key is gone (audit §4.12).
    assert documents[0].filename == "01_travel_standards.txt"
    assert documents[0].metadata["document_version"]


def test_tokenize_keeps_exact_chinese_concepts_as_ngrams():
    tokens = _tokenize("宠物寄养费，能报销吗？")

    assert "宠物寄养" in tokens
    assert "寄养费" in tokens
    assert "费能" not in tokens


def test_split_text_keeps_small_paragraphs_together():
    text = "第一段\n\n第二段\n\n第三段"

    chunks = split_text(text, max_chars=20, overlap=5)

    assert chunks == ["第一段\n\n第二段\n\n第三段"]


def test_split_text_keeps_faq_questions_as_separate_topics():
    text = "\n\n".join(
        [
            "Q9: 酒店价格超过标准怎么办？\nA9: 按标准报销。",
            "Q10: 到店后发现房间有问题怎么办？\nA10: 联系酒店处理。",
            "Q12: 出差期间的所有餐费都能报销吗？\nA12: 午餐和晚餐每餐不超过100元。",
        ]
    )

    chunks = split_text(text, max_chars=600, overlap=100)

    assert len(chunks) == 3
    assert chunks[-1].startswith("Q12")
    assert "Q9" not in chunks[-1]


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
