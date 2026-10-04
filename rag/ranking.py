"""Backend-neutral tokenization, fusion, reranking, and relevance filtering."""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List

import jieba

_DOMAIN_TERMS = (
    "住宿标准", "住宿费", "交通费", "火车票", "机票", "餐补", "餐费",
    "差旅申请", "报销", "发票",
)
_SYNONYMS = {
    "酒店": "住宿", "房费": "住宿费", "饭补": "餐补",
    "吃饭": "餐费", "高铁票": "火车票",
}
_STOPWORDS = {"帮我", "查一下", "相关", "请问", "一下", "怎么", "什么"}
_JIEBA = jieba.Tokenizer()
for _word in (*_DOMAIN_TERMS, *_SYNONYMS, *_STOPWORDS):
    _JIEBA.add_word(_word)


def fuse_results(
    vector_docs: List[Dict[str, Any]], bm25_docs: List[Dict[str, Any]], top_k: int,
) -> List[Dict[str, Any]]:
    rrf_k = 60.0
    merged: Dict[str, Dict[str, Any]] = {}
    for rank, doc in enumerate(vector_docs, start=1):
        merged[str(doc.get("id"))] = {
            "id": doc.get("id", ""), "content": doc.get("content", ""),
            "metadata": doc.get("metadata", {}), "distance": doc.get("distance"),
            "vector_rank": rank, "bm25_rank": None,
            "fusion_score": 1.0 / (rrf_k + rank),
        }
    for rank, doc in enumerate(bm25_docs, start=1):
        key = str(doc.get("id"))
        merged.setdefault(key, {
            "id": doc.get("id", ""), "content": doc.get("content", ""),
            "metadata": doc.get("metadata", {}), "distance": None,
            "vector_rank": None, "bm25_rank": rank, "fusion_score": 0.0,
        })
        merged[key]["bm25_rank"] = rank
        merged[key]["bm25_score"] = doc.get("bm25_score")
        merged[key]["fusion_score"] += 1.0 / (rrf_k + rank)
    return sorted(merged.values(), key=lambda doc: doc["fusion_score"], reverse=True)[:top_k]


def rerank_results(docs: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    terms = _rerank_terms(query)
    query_ngrams = _query_ngrams(query)
    focus_terms = _focus_terms(query)
    ngram_df = {
        ngram: sum(1 for doc in docs if ngram in doc.get("content", ""))
        for ngram in query_ngrams
    }

    def score(doc: Dict[str, Any]) -> float:
        content = doc.get("content", "")
        matches = sum(1 for term in terms if term in content)
        ngram_bonus = sum(
            0.015 * (1.0 + math.log((len(docs) + 1.0) / (ngram_df[term] + 1.0)))
            for term in query_ngrams if term in content
        )
        focus_bonus = sum(
            (0.30 if index == 0 else 0.18)
            for index, term in enumerate(focus_terms) if term in content
        )
        title = str((doc.get("metadata") or {}).get("title", ""))
        return (
            float(doc.get("fusion_score", 0.0))
            + matches * 0.04
            + sum(1 for term in terms if term in title) * 0.02
            + min(ngram_bonus, 0.30)
            + focus_bonus
            - _off_topic_penalty(query, content)
        )

    for doc in docs:
        doc["rerank_score"] = score(doc)
    return sorted(docs, key=lambda doc: doc.get("rerank_score", 0.0), reverse=True)


def filter_relevant_results(docs: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    terms = _rerank_terms(query)
    if not terms:
        return docs
    return [doc for doc in docs if any(term in doc.get("content", "") for term in terms)]


def _rerank_terms(query: str) -> List[str]:
    terms = [
        term for term in ("餐补", "餐费", "餐饮", "早餐", "午餐", "晚餐", "报销", "个人零食", "酒水")
        if term in query
    ]
    if any(term in query for term in ("餐补", "饭补", "吃饭")):
        terms.extend(["餐费", "餐饮", "早餐", "午餐", "晚餐", "报销"])
    if any(term in query for term in ("住宿", "酒店", "房费")):
        terms.extend(["住宿标准", "住宿费", "住宿上限", "酒店"])
    return list(dict.fromkeys(terms))


def _off_topic_penalty(query: str, content: str) -> float:
    if not any(term in query for term in ("餐补", "餐费", "餐饮", "饭补", "吃饭")):
        return 0.0
    meal_terms = ("餐费", "餐饮", "早餐", "午餐", "晚餐", "业务招待", "个人零食", "饮料", "酒水")
    if sum(1 for term in meal_terms if term in content) >= 2:
        return 0.0
    international_query = any(
        term in query for term in (
            "国际", "境外", "国外", "港澳", "新加坡", "日本", "韩国", "美国", "加拿大",
            "英国", "法国", "德国", "澳大利亚", "阿联酋",
        )
    )
    unrelated = ["家属", "升级酒店", "升级机票", "签证", "护照", "里程", "积分"]
    unrelated.append("国内出差" if international_query else "国际出差")
    return sum(1 for term in unrelated if term in content) * 0.03


def _query_ngrams(text: str) -> List[str]:
    runs = re.findall(r"[\u4e00-\u9fff]+", (text or "").lower())
    return list(dict.fromkeys(
        run[start : start + width]
        for run in runs for width in (3, 4)
        for start in range(0, len(run) - width + 1)
    ))


def _focus_terms(text: str) -> List[str]:
    normalized = (text or "").lower()
    for phrase in (
        "是多少", "有没有", "出差期间", "出差途中", "是否可以报销", "可以报销吗", "是否可报销",
        "能不能报销", "能否报销", "可以报销", "报销吗", "出差", "差旅", "标准", "多少",
        "怎么", "如何", "怎样", "什么", "哪些", "是否", "能否", "可以", "报销", "请问",
    ):
        normalized = normalized.replace(phrase, " ")
    generic = {"费用", "标准", "流程", "规定", "政策", "员工", "公司"}
    return [
        run for run in re.findall(r"[\u4e00-\u9fff]+", normalized)
        if len(run) >= 2 and run not in generic
    ]


def _tokenize(text: str) -> List[str]:
    """Use the same small domain vocabulary for BM25 documents and queries."""
    normalized = (text or "").lower()
    for variant, canonical in _SYNONYMS.items():
        normalized = normalized.replace(variant, canonical)
    return [
        token for token in _JIEBA.cut(normalized)
        if token not in _STOPWORDS
        and re.fullmatch(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", token)
    ]
