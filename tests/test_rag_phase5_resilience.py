"""Phase 5 regression tests (audit §11 Phase 5: 检索基础设施, 轻量交付).

Scope delivered (non-scheduled items kept as seams, not built out):
  * embedding 请求增加有限指数退避重试（瞬时失败，不重试 4xx 鉴权错误）；
  * embedding 进程内 LRU 缓存（同一文本不重复付费调用）；
  * BM25 全表扫描 Python 实现迁移到 ``SparseIndex`` 接缝，分数与旧内联实现
    逐位一致，原生 sparse 后端只差一个配置值。

- 瞬时 5xx/429/连接错误 → 重试成功     → ``test_embedder_retries_transient_then_succeeds``
- 非瞬时 401 → 立即失败不重试          → ``test_embedder_does_not_retry_auth_errors``
- 全部重试耗尽 → 抛出最后一次异常       → ``test_embedder_exhausts_retries_then_raises``
- 退避时间按指数增长                   → ``test_embedder_backoff_is_exponential``
- 相同文本只调用一次 API               → ``test_embedder_cache_serves_repeat_texts``
- 返回副本，外部改动不污染缓存          → ``test_embedder_cache_returns_copies``
- cache_size=0 关闭缓存                → ``test_embedder_cache_disabled``
- LRU 淘汰最旧条目                     → ``test_embedder_cache_evicts_lru``
- BM25 结果与旧内联算法逐位一致         → ``test_sparse_bm25_matches_legacy_algorithm``
- 空语料/空查询 → 空结果               → ``test_sparse_empty_inputs``
- 未知后端 fail-fast                   → ``test_sparse_unknown_backend_raises``
- 配置接线                             → ``test_config_threads_phase5_knobs``
"""
from __future__ import annotations

import math

import pytest
import requests

from rag.embedder import SiliconFlowEmbedder
from rag.sparse import PythonBM25SparseIndex, create_sparse_index


# ---- fake HTTP plumbing ----------------------------------------------------


def _embedding_response(dimension: int = 2, fill: float = 0.1):
    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"index": 0, "embedding": [fill] * dimension}]}

    return _Response()


def _error_response(status: int):
    class _Response:
        status_code = status

        def raise_for_status(self):
            raise requests.exceptions.HTTPError(f"HTTP {status}", response=self)

        def json(self):
            return {}

    return _Response()


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, *args, **kwargs):
        self.posts.append(kwargs)
        if not self.responses:
            raise AssertionError("no response left for post call")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _embedder(*responses, **kwargs):
    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("dimension", 2)
    kwargs.setdefault("max_retries", 2)
    session = _FakeSession(responses)
    embedder = SiliconFlowEmbedder(session=session, **kwargs)
    return embedder, session


# ---- retry / backoff --------------------------------------------------------


def test_embedder_retries_transient_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr("rag.embedder.time.sleep", lambda s: sleeps.append(s))
    embedder, session = _embedder(
        _error_response(503),
        _error_response(500),
        _embedding_response(),
        max_retries=2,
    )
    result = embedder.embed_texts(["出差报销"])
    assert result == [[0.1, 0.1]]
    assert len(session.posts) == 3


def test_embedder_retries_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr("rag.embedder.time.sleep", lambda s: None)
    embedder, session = _embedder(_error_response(429), _embedding_response())
    embedder.embed_texts(["住宿标准"])
    assert len(session.posts) == 2


def test_embedder_retries_connection_error(monkeypatch):
    monkeypatch.setattr("rag.embedder.time.sleep", lambda s: None)
    embedder, session = _embedder(
        requests.exceptions.ConnectionError("upstream down"),
        _embedding_response(),
    )
    result = embedder.embed_texts(["交通标准"])
    assert result == [[0.1, 0.1]]
    assert len(session.posts) == 2


def test_embedder_does_not_retry_auth_errors():
    embedder, session = _embedder(_error_response(401))
    with pytest.raises(requests.exceptions.HTTPError):
        embedder.embed_texts(["内部资料"])
    assert len(session.posts) == 1


def test_embedder_exhausts_retries_then_raises(monkeypatch):
    monkeypatch.setattr("rag.embedder.time.sleep", lambda s: None)
    embedder, session = _embedder(
        _error_response(500),
        _error_response(502),
        _error_response(504),
        max_retries=2,
    )
    with pytest.raises(requests.exceptions.HTTPError):
        embedder.embed_texts(["报销流程"])
    assert len(session.posts) == 3  # initial + 2 retries


def test_embedder_backoff_is_exponential(monkeypatch):
    sleeps = []
    monkeypatch.setattr("rag.embedder.time.sleep", lambda s: sleeps.append(s))
    embedder, session = _embedder(
        _error_response(503),
        _error_response(503),
        _error_response(503),
        _embedding_response(),
        max_retries=3,
        retry_base_delay_sec=2.0,
        retry_max_delay_sec=10.0,
    )
    embedder.embed_texts(["国际差旅"])
    assert sleeps == [2.0, 4.0, 8.0]  # 2*2^0, 2*2^1, capped 2*2^2=8 < 10


def test_embedder_backoff_caps_at_max_delay(monkeypatch):
    sleeps = []
    monkeypatch.setattr("rag.embedder.time.sleep", lambda s: sleeps.append(s))
    embedder, session = _embedder(
        _error_response(503),
        _error_response(503),
        _error_response(503),
        _embedding_response(),
        max_retries=3,
        retry_base_delay_sec=5.0,
        retry_max_delay_sec=7.0,
    )
    embedder.embed_texts(["餐费标准"])
    assert sleeps == [5.0, 7.0, 7.0]  # 5, min(10,7), min(20,7)


# ---- cache ------------------------------------------------------------------


def test_embedder_cache_serves_repeat_texts():
    embedder, session = _embedder(_embedding_response())
    first = embedder.embed_texts(["城市住宿"])
    second = embedder.embed_texts(["城市住宿"])
    assert first == second
    assert len(session.posts) == 1  # second call served from cache


def test_embedder_cache_returns_copies():
    embedder, session = _embedder(_embedding_response())
    first = embedder.embed_texts(["城市住宿"])
    original = first[0][0]
    first[0][0] = 999.0  # caller mutates its copy
    again = embedder.embed_texts(["城市住宿"])
    assert again[0][0] == original  # cache not poisoned


def test_embedder_cache_disabled():
    embedder, session = _embedder(_embedding_response(), _embedding_response(), cache_size=0)
    embedder.embed_texts(["城市住宿"])
    embedder.embed_texts(["城市住宿"])
    assert len(session.posts) == 2


def test_embedder_cache_evicts_lru():
    embedder, session = _embedder(
        _embedding_response(fill=1.0),
        _embedding_response(fill=2.0),
        _embedding_response(fill=3.0),
        _embedding_response(fill=4.0),  # re-request of the evicted first text
        cache_size=2,
    )
    assert embedder.embed_texts(["a"]) == [[1.0, 1.0]]
    assert embedder.embed_texts(["b"]) == [[2.0, 2.0]]
    assert embedder.embed_texts(["c"]) == [[3.0, 3.0]]  # evicts "a"
    assert embedder.embed_texts(["a"]) == [[4.0, 4.0]]  # cache miss again
    assert len(session.posts) == 4


# ---- sparse index extension point ------------------------------------------


def _tokenize(text):
    # Must be the SAME tokenizer the SparseIndex uses (rag.milvus_store._tokenize
    # adds domain phrase tokens) — otherwise scores can't match bit-for-bit.
    from rag.milvus_store import _tokenize as _real_tokenize

    return _real_tokenize(text)


def _legacy_bm25(docs, query):
    """Pre-refactor inline BM25 from MilvusVectorStore.bm25_search (audit §4.9)."""
    tokenized = [_tokenize(doc["content"]) for doc in docs]
    query_tokens = _tokenize(query)
    n_docs = len(tokenized)
    doc_lengths = [len(tokens) for tokens in tokenized]
    avgdl = sum(doc_lengths) / n_docs if n_docs else 0.0
    if not docs or not query_tokens or avgdl <= 0:
        return []
    df = {}
    for tokens in tokenized:
        for token in set(tokens):
            df[token] = df.get(token, 0) + 1
    scored = []
    for index, tokens in enumerate(tokenized):
        tf = {}
        for token in tokens:
            tf[token] = tf.get(token, 0) + 1
        score = 0.0
        for qt in query_tokens:
            if qt not in tf:
                continue
            freq = tf[qt]
            doc_freq = df.get(qt, 0)
            idf = math.log(1.0 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))
            denom = freq + 1.5 * (1 - 0.75 + 0.75 * len(tokens) / avgdl)
            score += idf * (freq * 2.5 / denom)
        if score > 0:
            scored.append((index, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return [
        {**docs[index], "bm25_score": score, "bm25_rank": rank}
        for rank, (index, score) in enumerate(scored[:10], start=1)
    ]


def test_sparse_bm25_matches_legacy_algorithm():
    docs = [
        {"content": "一线城市住宿标准每晚600元，含税", "metadata": {"chunk_id": "a"}},
        {"content": "出差交通：高铁二等座实报实销", "metadata": {"chunk_id": "b"}},
        {"content": "业务招待餐费按人均标准执行", "metadata": {"chunk_id": "c"}},
        {"content": "超标30%需总经理审批", "metadata": {"chunk_id": "d"}},
    ]
    index = PythonBM25SparseIndex()
    index.index(docs)
    for query in ("住宿标准", "高铁 报销", "审批", "餐费", "火星"):
        assert index.search(query) == _legacy_bm25(docs, query), query


def test_sparse_create_index_indexes_and_searches():
    docs = [{"content": "高铁一等座价格标准", "metadata": {}}]
    index = create_sparse_index("python", docs=docs)
    results = index.search("高铁")
    assert results[0]["content"] == docs[0]["content"]
    assert results[0]["bm25_rank"] == 1
    assert results[0]["bm25_score"] > 0


def test_sparse_empty_inputs():
    index = PythonBM25SparseIndex()
    index.index([{"content": "高铁标准", "metadata": {}}])
    assert index.search("") == []
    index.index([])
    assert index.search("高铁") == []
    assert create_sparse_index("python").search("任意") == []


def test_sparse_unknown_backend_raises():
    with pytest.raises(ValueError, match="Unsupported BM25/sparse backend"):
        create_sparse_index("native_sparse")
    with pytest.raises(ValueError, match="Unsupported"):
        create_sparse_index("milvus_sparse")


def test_sparse_index_refresh_changes_corpus():
    index = PythonBM25SparseIndex()
    index.index([{"content": "酒店住宿标准", "metadata": {}}])
    first = index.search("酒店")
    assert first
    index.index([{"content": "国际航班舱位标准", "metadata": {}}])
    assert index.search("酒店") == []
    assert index.search("航班")


# ---- config wiring ----------------------------------------------------------


def test_config_threads_phase5_knobs():
    from rag.config import RAGPipelineConfig

    defaults = RAGPipelineConfig.from_settings({})
    assert defaults.bm25_backend == "python"
    assert defaults.embedding_max_retries == 2
    assert defaults.embedding_retry_base_delay_sec == 1.0
    assert defaults.embedding_cache_size == 1024

    overridden = RAGPipelineConfig.from_settings(
        {
            "bm25_backend": "native_sparse",
            "embedding_max_retries": 5,
            "embedding_retry_base_delay_sec": 0.5,
            "embedding_cache_size": 64,
        }
    )
    assert overridden.bm25_backend == "native_sparse"
    assert overridden.embedding_max_retries == 5
    assert overridden.embedding_retry_base_delay_sec == 0.5
    assert overridden.embedding_cache_size == 64


def test_milvus_adapter_forwards_phase5_knobs(monkeypatch):
    from rag.vector_store import MilvusVectorStore

    captured = {}

    class _Store:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("rag.milvus_store.MilvusKnowledgeStore", _Store)
    MilvusVectorStore(
        knowledge_base_path="/tmp/kb",
        collection_name="knowledge",
        embedding_model="model",
        embedding_max_retries=5,
        embedding_retry_base_delay_sec=0.25,
        embedding_retry_max_delay_sec=4.0,
        embedding_cache_size=64,
        sparse_backend="python",
    )

    assert captured["embedding_max_retries"] == 5
    assert captured["embedding_retry_base_delay_sec"] == 0.25
    assert captured["embedding_retry_max_delay_sec"] == 4.0
    assert captured["embedding_cache_size"] == 64
    assert captured["sparse_backend"] == "python"
