"""Small in-memory BM25 index for RAG chunks."""
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
    with BM25-style scores.
    """

    @abstractmethod
    def index(self, docs: List[Dict[str, Any]]) -> None:
        """Ingest/refresh the document set (``[{content, metadata, ...}]``)."""

    @abstractmethod
    def search(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        """Return matching docs enriched with ``bm25_score``/``bm25_rank``."""


class PythonBM25SparseIndex(SparseIndex):
    """Precompute document terms once; scan scores for each query."""

    def __init__(self) -> None:
        self._docs: List[Dict[str, Any]] = []
        self._term_frequencies: List[Dict[str, int]] = []
        self._doc_lengths: List[int] = []
        self._df: Dict[str, int] = {}
        self._n_docs: int = 0
        self._avgdl: float = 0.0

    def index(self, docs: List[Dict[str, Any]]) -> None:
        self._docs = list(docs)
        self._term_frequencies = []
        self._doc_lengths = []
        df: Dict[str, int] = {}
        for doc in self._docs:
            tokens = _tokenize(doc.get("content", ""))
            tf: Dict[str, int] = {}
            for token in tokens:
                tf[token] = tf.get(token, 0) + 1
            self._term_frequencies.append(tf)
            self._doc_lengths.append(len(tokens))
            for token in tf:
                df[token] = df.get(token, 0) + 1
        self._n_docs = len(self._docs)
        self._avgdl = sum(self._doc_lengths) / self._n_docs if self._n_docs else 0.0
        self._df = df

    def search(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        if not self._n_docs or not query or self._avgdl <= 0:
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scored: List[tuple[int, float]] = []
        for index, tf in enumerate(self._term_frequencies):
            score = 0.0
            for query_token in query_tokens:
                if query_token not in tf:
                    continue
                freq = tf[query_token]
                doc_freq = self._df.get(query_token, 0)
                idf = math.log(1.0 + (self._n_docs - doc_freq + 0.5) / (doc_freq + 0.5))
                denom = freq + BM25_K1 * (
                    1 - BM25_B + BM25_B * self._doc_lengths[index] / self._avgdl
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
