"""RAG knowledge agent for the ask-question skill.

This module is intentionally thin. Retrieval, Milvus access, embeddings, and
ranking live in the project-level ``rag`` package. The agent only adapts the
orchestrator input into a query, calls the retriever, and formats the answer.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agentscope.agent import AgentBase
from agentscope.message import Msg

project_root = Path(__file__).resolve().parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from rag.retriever import KnowledgeRetriever
from core.execution_budget import ExecutionLimitExceeded, consume_external_call
from settings import RAG_CONFIG
from utils.skill_loader import SkillLoader

logger = logging.getLogger(__name__)


class RAGKnowledgeAgent(AgentBase):
    """AgentScope adapter for RAG-based business-travel knowledge Q&A."""

    def __init__(
        self,
        name: str = "RAGKnowledgeAgent",
        model=None,
        knowledge_base_path: Optional[str] = None,
        collection_name: str = "business_travel_knowledge",
        embedding_model: str = "BAAI/bge-small-zh-v1.5",
        top_k: int = 3,
        skills_root: Optional[str] = None,
        **kwargs,
    ):
        super().__init__()
        self.name = name
        self.model = model
        self.skill_loader = SkillLoader(skills_root)

        self.retriever = KnowledgeRetriever(
            knowledge_base_path=knowledge_base_path or RAG_CONFIG.get("knowledge_base_path", "data/rag_knowledge"),
            collection_name=collection_name or RAG_CONFIG.get("collection_name", "business_travel_knowledge"),
            embedding_model=RAG_CONFIG.get("embedding_model", embedding_model),
            top_k=top_k,
        )
        self.initialized = self.retriever.initialized
        if not self.initialized:
            logger.error("RAG retriever initialization failed: %s", self.retriever.error)

    def add_documents(self, documents: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Add already prepared document chunks to the RAG store."""
        return self.retriever.add_documents(documents)

    def search_knowledge(self, query: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        """Search the RAG store through the shared retriever."""
        consume_external_call("rag")
        return self.retriever.search(query, top_k=top_k)

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        if not self.initialized:
            return self._msg(
                {
                    "status": "error",
                    "message": self.retriever.error or "RAG Agent not initialized",
                    "retrieved_documents": [],
                }
            )

        user_query = self._extract_query(x)
        if not user_query:
            return self._msg({"status": "no_knowledge", "query": "", "answer": "请先告诉我你想查询的问题。", "retrieved_documents": []})

        retrieved_docs = self.search_knowledge(user_query)
        if not retrieved_docs:
            stats = self.get_stats()
            if stats.get("status") == "success" and int(stats.get("total_documents", 0)) == 0:
                return self._msg(
                    {
                        "status": "knowledge_base_empty",
                        "query": user_query,
                        "answer": "知识库还没有完成入库。请先停止正在运行的 CLI/WebUI，然后执行 RAG 入库命令。",
                        "retrieved_documents": [],
                    }
                )
            return self._msg(
                {
                    "status": "no_knowledge",
                    "query": user_query,
                    "answer": "抱歉，我在知识库中没有找到相关信息。",
                    "retrieved_documents": [],
                }
            )

        knowledge_context = self._format_knowledge_context(retrieved_docs)
        if self.model:
            answer = await self._generate_answer(user_query, knowledge_context)
        else:
            answer = "以下是知识库中的相关信息：\n\n" + knowledge_context

        return self._msg(
            {
                "status": "success",
                "query": user_query,
                "answer": answer,
                "retrieved_documents": [self._serialize_doc(doc) for doc in retrieved_docs],
                "sources": [self._serialize_source(doc) for doc in retrieved_docs],
            }
        )

    def get_stats(self) -> Dict[str, Any]:
        return self.retriever.stats()

    def close(self) -> None:
        self.retriever.close()

    def _extract_query(self, x: Optional[Union[Msg, List[Msg]]]) -> str:
        if x is None:
            return ""

        content = x[-1].content if isinstance(x, list) and x else getattr(x, "content", "")
        if not isinstance(content, str):
            return str(content or "").strip()

        text = content.strip()
        if not text.startswith("{"):
            return text

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text

        context = data.get("context")
        previous_results = data.get("previous_results") or []
        trip = next(
            (
                (item.get("result") or {}).get("data")
                for item in reversed(previous_results)
                if item.get("agent_name") == "event_collection"
            ),
            None,
        )
        if isinstance(trip, dict) and trip.get("origin") and trip.get("destination"):
            dates = trip.get("start_date") or "日期待确认"
            duration = (
                f"{trip['duration_days']}天" if trip.get("duration_days")
                else trip.get("end_date") or "行程时长待确认"
            )
            return (
                f"请检索从{trip['origin']}到{trip['destination']}、{dates}、{duration}的公司出差，"
                "适用的住宿、交通、补贴、报销和审批制度。只返回公司制度证据，不提供路线规划。"
            )
        if isinstance(context, dict):
            query = context.get("rewritten_query") or context.get("user_query")
            if query:
                return str(query).strip()
        return str(data.get("rewritten_query") or data.get("query") or text).strip()

    def _format_knowledge_context(self, docs: List[Dict[str, Any]]) -> str:
        return "\n\n".join(f"【知识片段{i}】\n{doc.get('content', '')}" for i, doc in enumerate(docs, start=1))

    async def _generate_answer(self, user_query: str, knowledge_context: str) -> str:
        skill_instruction = self.skill_loader.get_skill_content("ask-question") or "请基于知识库中的信息回答用户的问题。"
        prompt = f"""你是一个商旅知识专家。请严格基于以下知识库信息回答用户问题。

【用户问题】
{user_query}

【知识库信息】
{knowledge_context}

【任务说明】
{skill_instruction}

【重要约束】
1. 先判断知识库信息与用户问题的关系：直接回答、相关政策、部分回答、无依据。
2. 只有在知识库信息完全没有相关依据时，才可以说“知识库中没有找到相关信息”。
3. 如果用户用词和知识库说法不完全一致，但检索片段能回答实际意图，要直接整理相关政策，不要说“没有相关信息”。
4. 如果知识库只缺少某个固定名称、固定金额或明确口径，但有相关标准/流程/条件，请说“知识库没有明确规定该说法，但相关规定是……”，不要使用“没有找到相关信息，但……”这类矛盾表达。
5. 不要根据模型自己的常识补充知识库之外的信息。
6. 回答要面向用户总结：先给结论，再列依据/标准，最后补充限制或例外；不要直接堆叠原文。
"""

        try:
            response = await self.model(
                [
                    {"role": "system", "content": "你是一个商旅知识专家。"},
                    {"role": "user", "content": prompt},
                ]
            )
            answer = await self._extract_model_text(response) or "无法生成答案"
            return self._normalize_answer(answer)
        except ExecutionLimitExceeded:
            raise
        except Exception as exc:
            logger.error("Error generating RAG answer with LLM: %s", exc)
            return "知识库已检索到相关信息，但生成面向用户的总结回答时出错，请稍后重试。"

    async def _extract_model_text(self, response: Any) -> str:
        if self._is_async_iterable(response):
            text = ""
            async for chunk in response:
                chunk_text = self._extract_chunk_text(chunk)
                if chunk_text:
                    text = self._merge_stream_text(text, chunk_text)
            return text.strip()

        return self._extract_chunk_text(response)

    def _merge_stream_text(self, current: str, incoming: str) -> str:
        if not current:
            return incoming
        if incoming.startswith(current):
            return incoming
        if current.endswith(incoming):
            return current
        return current + incoming

    def _normalize_answer(self, answer: str) -> str:
        text = (answer or "").strip()
        if not text:
            return text
        if not self._has_no_info_claim(text) or not self._has_related_policy_content(text):
            return text

        replacement = "知识库没有明确规定用户问题中的具体说法，但检索到相关规定："
        text = re.sub(
            r"^\s*(抱歉[，,]?\s*)?知识库中?(?:没有|未)(?:找到|检索到|提及|明确规定)?[^。；;\n]*?(?:相关信息|相关规定|相关内容|明确规定|提及)[。；;\n]*",
            replacement,
            text,
            count=1,
        )
        text = re.sub(
            r"^\s*(抱歉[，,]?\s*)?(?:没有|未)(?:找到|检索到|提及|明确规定)?[^。；;\n]*?(?:相关信息|相关规定|相关内容|明确规定|提及)[。；;\n]*",
            replacement,
            text,
            count=1,
        )
        return text.strip()

    def _has_no_info_claim(self, text: str) -> bool:
        patterns = (
            "没有找到",
            "没有检索到",
            "知识库中没有",
            "知识库没有",
            "未找到",
            "未检索到",
            "未提及",
            "没有明确规定",
        )
        return any(pattern in text for pattern in patterns)

    def _has_related_policy_content(self, text: str) -> bool:
        markers = (
            "但",
            "不过",
            "仅规定",
            "只规定",
            "相关规定",
            "相关政策",
            "标准",
            "流程",
            "要求",
            "报销",
            "审批",
            "申请",
            "提供",
            "不超过",
            "不予",
            "可",
            "需要",
        )
        return any(marker in text for marker in markers)

    def _extract_chunk_text(self, response: Any) -> str:
        if isinstance(response, dict):
            return self._extract_dict_text(response)

        text_value = self._safe_getattr(response, "text")
        if text_value is not None:
            return str(text_value).strip()

        content = self._safe_getattr(response, "content")
        if content is not None:
            return self._extract_content_text(content)

        return str(response or "").strip()

    def _extract_dict_text(self, response: Dict[str, Any]) -> str:
        direct = response.get("answer") or response.get("content") or response.get("text")
        if direct:
            return self._extract_content_text(direct)

        choices = response.get("choices")
        if isinstance(choices, list):
            texts: List[str] = []
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if isinstance(message, dict):
                    texts.append(self._extract_content_text(message.get("content")))
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    texts.append(self._extract_content_text(delta.get("content")))
                texts.append(self._extract_content_text(choice.get("text")))
            return "\n".join(text for text in texts if text).strip()

        return ""

    def _extract_content_text(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            texts: List[str] = []
            for item in content:
                if isinstance(item, dict):
                    texts.append(self._extract_content_text(item.get("text") or item.get("content")))
                else:
                    texts.append(self._extract_content_text(item))
            return "\n".join(text for text in texts if text).strip()
        if isinstance(content, dict):
            return self._extract_dict_text(content)
        return str(content).strip()

    def _safe_getattr(self, value: Any, name: str) -> Any:
        try:
            return getattr(value, name)
        except Exception:
            return None

    def _is_async_iterable(self, value: Any) -> bool:
        try:
            return callable(getattr(value, "__aiter__", None))
        except Exception:
            return False

    def _serialize_doc(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        content = doc.get("content", "")
        return {
            "content": content[:200] + "..." if len(content) > 200 else content,
            "metadata": doc.get("metadata", {}),
        }

    def _serialize_source(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        return {
            "file": metadata.get("source") or metadata.get("file_name") or metadata.get("filename") or "企业差旅知识库",
            "page": metadata.get("page") or metadata.get("page_number"),
            "section": metadata.get("section") or metadata.get("title"),
        }

    def _msg(self, content: Dict[str, Any]) -> Msg:
        return Msg(name=self.name, content=json.dumps(content, ensure_ascii=False), role="assistant")

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
