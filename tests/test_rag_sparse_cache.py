"""BM25 cache follows the active RAG snapshot without touching dense retrieval."""
from contextlib import contextmanager

from rag.hybrid import bm25_search
from rag.postgres_vector_store import PostgresVectorStore


class _Pool:
    def __init__(self):
        self.state = {"active_version": "v1", "updated_at": 1}

    @contextmanager
    def connection(self):
        yield self

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, query, params):
        assert "active_version, updated_at" in query

    def fetchone(self):
        return self.state


def test_bm25_reuses_index_until_corpus_or_scope_changes():
    store = PostgresVectorStore.__new__(PostgresVectorStore)
    store.pool = _Pool()
    store.collection_name = "demo"
    store.search_scopes = ()
    store.sparse_backend = "python"
    store.bm25_top_k = 10
    fetched = []

    def fetch(version=None):
        fetched.append((version, store.search_scopes))
        return [{"id": 1, "content": "住宿费报销", "metadata": {}}]

    store.fetch_all_documents = fetch

    assert bm25_search(store, "房费")
    assert bm25_search(store, "住宿费")
    assert fetched == [("v1", ())]

    another_store = PostgresVectorStore.__new__(PostgresVectorStore)
    another_store.pool = store.pool
    another_store.collection_name = store.collection_name
    another_store.search_scopes = store.search_scopes
    another_store.sparse_backend = store.sparse_backend
    another_store.bm25_top_k = store.bm25_top_k
    another_store.fetch_all_documents = fetch
    assert bm25_search(another_store, "房费")
    assert fetched == [("v1", ())]

    store.pool.state = {"active_version": "v2", "updated_at": 2}
    assert bm25_search(store, "房费")
    assert fetched == [("v1", ()), ("v2", ())]

    store.search_scopes = ("finance",)
    assert bm25_search(store, "房费")
    assert fetched[-1] == ("v2", ("finance",))

    store.pool.state = {"active_version": "v2", "updated_at": 3}
    assert bm25_search(store, "房费")
    assert len(fetched) == 4
