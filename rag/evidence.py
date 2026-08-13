"""Universal evidence gate for RAG answers (audit Phase 4).

A deterministic, scale-free classifier that decides whether the retrieved
evidence is enough to answer a query, not enough (``insufficient`` →
no-knowledge), or only partially covering it (``partial`` → hedged answer).
The gate applies to every query — not just negation ones — replacing the old
"some docs were returned ⇒ answer confidently" behavior.

Signals (chosen to be backend-agnostic: the same thresholds hold for Milvus
RRF fusion scores and the in-memory store's character-count scores):

  * ``coverage``      — fraction of query tokens (CJK bigrams + latin words +
                        domain rerank terms) present in the top-N chunk
                        contents.  Scale-free, 0..1.
  * ``rerank_lift``   — max(``rerank_score``) − max(``fusion_score``) over the
                        top-N results.  This isolates the deterministic boost
                        from term/title/ngram/focus matches, which is bounded
                        (≤ ~1.0) regardless of the fusion-score scale.

Calibrated on the golden-query set (``tests/data/golden_queries.json``) against
the ``data/documents`` corpus (36 queries, 6 intents).  Calibration findings:

  * exact policy phrasings reach ``coverage`` ≥ 0.40 (mean 0.75);
  * genuine no-answer queries (宠物寄养/健身房会员) sit at ``coverage``
    0.23–0.38 with a *high* ``rerank_score`` — generic terms like "报销" match
    broadly, so rerank score alone must never grant ``sufficient``;
  * colloquial/negation queries that do have an answer in the corpus overlap
    the policy lexicon and carry a real rerank lift from domain terms;
  * zero-coverage, zero-lift queries (e.g. 火星出差) are ``insufficient``.

The remaining middle is deliberately ``partial``: the answer may still be
produced from the retrieved evidence, but the caller must hedge and must not
fabricate policy that is not present.  This is what stops confident
hallucinations for out-of-scope questions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Reuse the project's domain-term vocabulary rather than duplicating it; the
# module has no heavy imports (pymilvus is lazy), so this stays dependency-free.
from .milvus_store import _rerank_terms

INSUFFICIENT_COVERAGE = 0.05  # below this, no meaningful evidence overlap
SUFFICIENT_COVERAGE = 0.40  # strong lexical overlap ⇒ direct answer
LIFT_MIN_COVERAGE = 0.25  # lower coverage is rescued only by real term lifts
SUFFICIENT_LIFT = 0.15  # rerank term/title/ngram/focus boost confirming evidence

_CJK_RUN = re.compile(r"[一-鿿]+")
_LATIN_WORD = re.compile(r"[a-z0-9_]+")
_RETRIEVAL_ONLY_TERMS = frozenset({"住宿标准", "住宿费", "住宿上限", "酒店"})


def _cjk_bigrams(text: str) -> List[str]:
    """CJK 2-grams — the tokenizer that actually covers short policy queries.

    The rerank pipeline's 3–4 gram ngrams (``_query_ngrams``) are too long for
    evidence coverage of short questions like "报销流程"; 2-grams capture the
    overlap.  ``_query_ngrams`` stays as a *longer exact-match bonus*.
    """
    runs = _CJK_RUN.findall((text or "").lower())
    grams = []
    for run in runs:
        for i in range(len(run) - 1):
            grams.append(run[i : i + 2])
    return list(dict.fromkeys(grams))


def _latin_words(text: str) -> List[str]:
    return _LATIN_WORD.findall((text or "").lower())


def _query_tokens(query: str) -> List[str]:
    """The evidence token set: CJK bigrams + latin words + domain rerank terms."""
    domain_terms = [
        term for term in _rerank_terms(query) if term not in _RETRIEVAL_ONLY_TERMS
    ]
    return list(dict.fromkeys(_cjk_bigrams(query) + _latin_words(query) + domain_terms))


def _doc_text(doc: Dict[str, Any]) -> str:
    content = doc.get("content") or ""
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    display = metadata.get("display_text") or doc.get("display_text") or ""
    return f"{content}\n{display}"


@dataclass(frozen=True)
class EvidenceVerdict:
    """Outcome of the evidence gate for one (query, retrieved docs) pair."""

    verdict: str  # "sufficient" | "partial" | "insufficient"
    score: float  # primary numeric score driving the decision (coverage)
    coverage: float = 0.0
    rerank_lift: float = 0.0
    matched_terms: List[str] = field(default_factory=list)
    total_terms: int = 0
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "score": self.score,
            "coverage": self.coverage,
            "rerank_lift": self.rerank_lift,
            "matched_terms": self.matched_terms,
            "total_terms": self.total_terms,
            "reasons": self.reasons,
        }


def evaluate_evidence(
    query: str,
    docs: List[Dict[str, Any]],
    *,
    top_n: int = 3,
    min_terms: int = 2,
) -> EvidenceVerdict:
    """Classify how well ``docs`` evidence answers ``query``.

    ``docs`` are the dicts produced by ``Retriever.search``/``to_dict`` (with
    ``content``, optional ``rerank_score``/``fusion_score``).  They are ranked
    by ``rerank_score`` and only the top ``top_n`` are considered.
    """
    if not docs:
        return EvidenceVerdict(
            verdict="insufficient",
            score=0.0,
            reasons=["没有检索到任何知识库片段"],
        )

    ranked = sorted(
        docs,
        key=lambda d: float(d.get("rerank_score") or 0.0),
        reverse=True,
    )
    evidence = ranked[:top_n]

    tokens = _query_tokens(query)
    if len(tokens) < min_terms:
        # A degenerate query (single short term) can't be judged by coverage;
        # fall back to the rerank lift, else hedge.
        lift = _rerank_lift(evidence)
        verdict = "sufficient" if lift >= SUFFICIENT_LIFT else "partial"
        return EvidenceVerdict(
            verdict=verdict,
            score=lift,
            coverage=0.0,
            rerank_lift=lift,
            matched_terms=[],
            total_terms=len(tokens),
            reasons=[f"查询过短（{len(tokens)} 个证据词），按 rerank 提升判定"],
        )

    joined = "\n".join(_doc_text(doc) for doc in evidence)
    matched = [token for token in tokens if token in joined]
    coverage = len(matched) / len(tokens)
    lift = _rerank_lift(evidence)

    reasons: List[str] = []
    if coverage < INSUFFICIENT_COVERAGE:
        verdict = "insufficient"
        reasons.append(f"证据覆盖率 {coverage:.0%} 低于阈值 {INSUFFICIENT_COVERAGE:.0%}")
    elif coverage >= SUFFICIENT_COVERAGE:
        verdict = "sufficient"
        reasons.append(f"证据覆盖率 {coverage:.0%} 达到阈值 {SUFFICIENT_COVERAGE:.0%}")
    elif coverage >= LIFT_MIN_COVERAGE and lift >= SUFFICIENT_LIFT:
        verdict = "sufficient"
        reasons.append(f"证据覆盖率 {coverage:.0%} + rerank 提升 {lift:.2f} 确认命中")
    else:
        verdict = "partial"
        reasons.append(
            f"证据覆盖率 {coverage:.0%} 处于模糊区间，rerank 提升 {lift:.2f}，需带兜底措辞"
        )

    return EvidenceVerdict(
        verdict=verdict,
        score=coverage,
        coverage=coverage,
        rerank_lift=lift,
        matched_terms=matched,
        total_terms=len(tokens),
        reasons=reasons,
    )


def _rerank_lift(docs: List[Dict[str, Any]]) -> float:
    """Boost over the fusion baseline: max(rerank) − max(fusion) on top-N.

    Both stores put the base similarity in ``fusion_score`` and let
    ``rerank_score`` add a bounded deterministic boost; the difference is
    scale-free and measures exactly the term/title/ngram/focus evidence match.
    """
    best_rerank = max(float(d.get("rerank_score") or 0.0) for d in docs)
    best_fusion = max(float(d.get("fusion_score") or 0.0) for d in docs)
    return best_rerank - best_fusion
