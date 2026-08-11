from pathlib import Path

import pytest

from rag.chunker import split_text
from rag.document_loader import load_text_documents
from rag.embedder import SiliconFlowEmbedder
from rag.milvus_store import MilvusKnowledgeStore, _tokenize, fuse_results, rerank_results
from rag.retriever import expand_query
from rag.schemas import DocumentChunk


def test_load_text_documents_reads_txt_files(tmp_path: Path):
    doc = tmp_path / "01_travel_standards.txt"
    doc.write_text("Travel Standards\n\n北京住宿标准。", encoding="utf-8")

    documents = load_text_documents(str(tmp_path))

    assert len(documents) == 1
    assert documents[0].title == "Travel Standards"
    assert documents[0].category == "travel_policy"
    assert documents[0].metadata["parent_doc"] == "01_travel_standards.txt"


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


class _FakeEmbedder:
    def embed_texts(self, texts):
        return [[float(index), 0.5] for index, _text in enumerate(texts, start=1)]


class _FakeMilvusClient:
    def __init__(self, *, fail_promotion=False):
        self.collections = {"knowledge": [{"id": 1, "content": "old"}]}
        self.fail_promotion = fail_promotion

    def create_collection(self, collection_name, **_kwargs):
        self.collections[collection_name] = []

    def insert(self, collection_name, data):
        self.collections[collection_name].extend(data)

    def get_collection_stats(self, collection_name):
        return {"row_count": len(self.collections[collection_name])}

    def has_collection(self, collection_name):
        return collection_name in self.collections

    def rename_collection(self, old_name, new_name):
        if self.fail_promotion and "__staging_" in old_name and new_name == "knowledge":
            self.fail_promotion = False
            raise RuntimeError("promotion failed")
        self.collections[new_name] = self.collections.pop(old_name)

    def drop_collection(self, collection_name):
        self.collections.pop(collection_name)

    def load_collection(self, _collection_name):
        return None


def _atomic_store(client):
    store = object.__new__(MilvusKnowledgeStore)
    store.client = client
    store.collection_name = "knowledge"
    store.embedding_dim = 2
    store.embedding_model = _FakeEmbedder()
    return store


def _chunk(content):
    return DocumentChunk(
        content=content,
        source_path=f"/{content}.txt",
        filename=f"{content}.txt",
        file_type="txt",
        page_number=None,
        chunk_index=1,
        content_type="text",
        hash=content,
    )


def test_atomic_milvus_refresh_promotes_complete_staging_collection():
    client = _FakeMilvusClient()

    result = _atomic_store(client).replace_chunks_atomically([_chunk("new-1"), _chunk("new-2")])

    assert result["total_count"] == 2
    assert [row["content"] for row in client.collections["knowledge"]] == ["new-1", "new-2"]
    assert set(client.collections) == {"knowledge"}


def test_atomic_milvus_refresh_restores_live_collection_when_promotion_fails():
    client = _FakeMilvusClient(fail_promotion=True)

    with pytest.raises(RuntimeError, match="promotion failed"):
        _atomic_store(client).replace_chunks_atomically([_chunk("new")])

    assert client.collections["knowledge"] == [{"id": 1, "content": "old"}]
    assert set(client.collections) == {"knowledge"}


def test_rebuild_collection_recovers_from_windows_manifest_replace_error(tmp_path: Path):
    collection_name = "business_travel_knowledge"
    collection_dir = tmp_path / "collections" / collection_name
    collection_dir.mkdir(parents=True)
    (collection_dir / "manifest.json").write_text("old", encoding="utf-8")
    (collection_dir / "manifest.json.tmp").write_text("tmp", encoding="utf-8")

    class DropFailingClient:
        def has_collection(self, name):
            return True

        def drop_collection(self, name):
            raise RuntimeError(
                "[WinError 183] Cannot create a file when that file already exists: "
                "'manifest.json.tmp' -> 'manifest.json'"
            )

        def close(self):
            pass

    class ResetClient:
        def __init__(self):
            self.created = False

        def has_collection(self, name):
            return False

        def create_collection(self, **kwargs):
            self.created = True

        def load_collection(self, name):
            pass

        def close(self):
            pass

    store = object.__new__(MilvusKnowledgeStore)
    store.client = DropFailingClient()
    store.collection_name = collection_name
    store.embedding_dim = 384
    store.milvus_uri = str(tmp_path)
    reset_client = ResetClient()
    store._reset_client = lambda: setattr(store, "client", reset_client)

    store.rebuild_collection()

    assert not collection_dir.exists()
    assert reset_client.created is True


def test_rebuild_collection_prepares_windows_manifest_before_drop(tmp_path: Path, monkeypatch):
    collection_name = "business_travel_knowledge"
    collection_dir = tmp_path / "collections" / collection_name
    collection_dir.mkdir(parents=True)
    manifest_path = collection_dir / "manifest.json"
    manifest_path.write_text("old", encoding="utf-8")

    class DropSucceedsClient:
        def __init__(self):
            self.saw_manifest_at_drop = None
            self.created = False

        def has_collection(self, name):
            return True

        def drop_collection(self, name):
            self.saw_manifest_at_drop = manifest_path.exists()

        def create_collection(self, **kwargs):
            self.created = True

        def load_collection(self, name):
            pass

        def close(self):
            pass

    monkeypatch.setattr("rag.milvus_store.os.name", "nt")
    client = DropSucceedsClient()
    store = object.__new__(MilvusKnowledgeStore)
    store.client = client
    store.collection_name = collection_name
    store.embedding_dim = 384
    store.milvus_uri = str(tmp_path)

    store.rebuild_collection()

    assert client.saw_manifest_at_drop is False
    assert client.created is True
