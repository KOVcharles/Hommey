"""Sparse/BM25 ranking behind a swappable interface (audit Phase 5).

The roadmap scopes BM25 work as "按触发条件评估，非排期": a full-scan Python
BM25 is fine until the corpus grows large enough that per-query Python
tokenization/scoring becomes the bottleneck.  That trigger is not reached yet,
so no native backend is implemented here — but the seam is: ``SparseIndex`` is
the extension point a precomputed / native-sparse backend (e.g. Milvus
SparseVector, a persisted inverted index) implements later, and
``MilvusVectorStore`` routes through it via ``create_sparse_index``.  Switching
backends is then a config value, not a rewrite of ``hybrid_search``/RRF fusion.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from .ranking import _tokenize

BM25_K1 = 1.5
BM25_B = 0.75


class SparseIndex(ABC):
    """Extension point for sparse/keyword ranking.

    Implementations hold an indexable document set and answer keyword queries
    with BM25-style scores.  The default Python implementation re-tokenizes the
    whole set on every ``index`` call — the full-scan baseline the roadmap
    expects to outgrow — while a future backend precomputes the inverted index.
    """

    @abstractmethod
    def index(self, docs: List[Dict[str, Any]]) -> None:
        """Ingest/refresh the document set (``[{content, metadata, ...}]``)."""

    @abstractmethod
    def search(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        """Return matching docs enriched with ``bm25_score``/``bm25_rank``."""


class PythonBM25SparseIndex(SparseIndex):
    """The current full-scan Python BM25, kept score-identical to the previous
    inline implementation in ``MilvusVectorStore.bm25_search`` (audit §4.9)."""

    def __init__(self) -> None:
        self._docs: List[Dict[str, Any]] = []
        self._tokenized: List[List[str]] = []
        self._doc_lengths: List[int] = []
        self._df: Dict[str, int] = {}
        self._n_docs: int = 0
        self._avgdl: float = 0.0

    def index(self, docs: List[Dict[str, Any]]) -> None:
        self._docs = list(docs)
        self._tokenized = [_tokenize(doc.get("content", "")) for doc in self._docs]
        self._doc_lengths = [len(tokens) for tokens in self._tokenized]
        self._n_docs = len(self._tokenized)
        self._avgdl = sum(self._doc_lengths) / self._n_docs if self._n_docs else 0.0
        df: Dict[str, int] = {}
        for tokens in self._tokenized:
            for token in set(tokens):
                df[token] = df.get(token, 0) + 1
        self._df = df

    def search(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        if not self._n_docs or not query or self._avgdl <= 0:
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scored: List[tuple[int, float]] = []
        for index, tokens in enumerate(self._tokenized):
            tf: Dict[str, int] = {}
            for token in tokens:
                tf[token] = tf.get(token, 0) + 1

            score = 0.0
            for query_token in query_tokens:
                if query_token not in tf:
                    continue
                freq = tf[query_token]
                doc_freq = self._df.get(query_token, 0)
                idf = math.log(1.0 + (self._n_docs - doc_freq + 0.5) / (doc_freq + 0.5))
                denom = freq + BM25_K1 * (
                    1 - BM25_B + BM25_B * len(tokens) / self._avgdl
                )
                score += idf * (freq * (BM25_K1 + 1) / denom)

            if score > 0:
                scored.append((index, score))

        scored.sort(key=lambda item: item[1], reverse=True)
        results: List[Dict[str, Any]] = []
        for rank, (index, score) in enumerate(scored[:top_k], start=1):
            doc = self._docs[index]
            results.append({**doc, "bm25_score": score, "bm25_rank": rank})
        return results


def create_sparse_index(
    backend: str = "python",
    docs: List[Dict[str, Any]] | None = None,
) -> SparseIndex:
    """Factory for the configured sparse backend.

    ``backend`` is the extension point the roadmap's native-sparse trigger
    would plug into (``HOMMEY_RAG_BM25_BACKEND``); today only ``"python"``
    exists, and an unknown value fails loudly rather than silently degrading.
    """
    normalized = (backend or "python").lower()
    if normalized == "python":
        index: SparseIndex = PythonBM25SparseIndex()
    else:
        raise ValueError(f"Unsupported BM25/sparse backend: {backend}")
    if docs is not None:
        index.index(docs)
    return index
