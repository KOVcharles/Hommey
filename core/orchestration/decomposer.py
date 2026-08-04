"""LLM-assisted task decomposition with a deterministic phase-one fallback."""
from __future__ import annotations

from datetime import datetime
import json
import logging
import re
from typing import Any, Dict, List

from core.execution_budget import ExecutionLimitExceeded, consume_agent_call
from core.intent_result import parse_json_object
from core.llm_response import extract_text_from_response

logger = logging.getLogger(__name__)

_DATE_PATTERN = re.compile(r"(今天|明天|后天|这两天|未来两天|未来几天|本周|下周|\d{1,2}月\d{1,2}日)")
_DESTINATION_PATTERNS = (
    re.compile(r"(?:去|到|前往)([\u4e00-\u9fff]{2,10}?)(?:市)?(?:出差|差旅)"),
    re.compile(
        r"(?:查|查询|看看|了解)?(?:一下)?(?:今天|明天|后天|这两天|未来两天|未来几天)?"
        r"([\u4e00-\u9fff]{2,8}?)(?:市)?(?:的)?天气"
    ),
)


class TaskDecomposer:
    """Turn a multi-intent request into semantic tasks, never executable agents."""

    def __init__(self, model=None):
        self.model = model

    async def decompose(self, query: str, intention_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        intents = [
            item.get("type")
            for item in intention_data.get("intents", [])
            if item.get("should_call_skill")
        ]
        if self.model is None:
            return self.fallback(query, intents)

        prompt = self._prompt(query, intents, intention_data.get("key_entities") or {})
        try:
            consume_agent_call("TaskDecomposer")
            response = await self.model([
                {
                    "role": "system",
                    "content": (
                        "你负责把已识别的公司差旅意图拆成互不越界的语义任务。"
                        "只输出JSON；不得选择Agent、工具或扩大用户请求。"
                    ),
                },
                {"role": "user", "content": prompt},
            ])
            text = await extract_text_from_response(response)
            payload = parse_json_object(text)
            tasks = payload.get("tasks")
            if not isinstance(tasks, list):
                raise ValueError("decomposer response has no tasks list")
            return tasks
        except ExecutionLimitExceeded:
            raise
        except Exception as exc:
            logger.warning("Task decomposition failed; using deterministic fallback: %s", exc)
            return self.fallback(query, intents)

    @staticmethod
    def _prompt(query: str, intents: List[str], entities: Dict[str, Any]) -> str:
        today = datetime.now().astimezone().date().isoformat()
        return f"""【当前日期】{today}
【原始问题】{query}
【已授权意图】{json.dumps(intents, ensure_ascii=False)}
【已识别实体】{json.dumps(entities, ensure_ascii=False)}

为每个已授权意图生成且只生成一个任务。每个 query 只保留该意图负责的内容：
- rag_knowledge 只查询公司制度、标准、补贴、住宿、交通、报销或审批；不要提天气。
- information_query 只查询目的地天气或公开交通信息；不要询问公司制度。
- 保留用户原本的查询范围；用户只泛问“差旅标准”时不要自行枚举住宿、交通、补贴、报销或审批。
- 不得增加已授权意图之外的任务。
- 相对日期结合当前日期改写清楚；不确定的信息保持原表达，不要猜测。

只输出：
{{
  "tasks": [
    {{
      "task_id": "稳定的小写英文标识",
      "intent": "已授权意图",
      "query": "该任务独立、完整的中文查询",
      "entities": {{}},
      "depends_on": [],
      "side_effect": false,
      "failure_policy": "continue",
      "display_order": 0
    }}
  ]
}}"""

    @classmethod
    def fallback(cls, query: str, intents: List[str]) -> List[Dict[str, Any]]:
        """Phase-one projection used when decomposition LLM is unavailable or invalid."""
        destination = cls._extract_destination(query)
        date_phrase = cls._extract_date(query)
        tasks = []
        order = 0
        if "rag_knowledge" in intents:
            subject = f"{destination}出差" if destination else "本次国内出差"
            tasks.append({
                "task_id": "policy",
                "intent": "rag_knowledge",
                "query": f"查询{subject}适用的公司差旅标准",
                "entities": {"destination": destination} if destination else {},
                "depends_on": [],
                "side_effect": False,
                "failure_policy": "continue",
                "display_order": order,
            })
            order += 1
        if "information_query" in intents:
            location = destination or "目的地"
            period = date_phrase or "近期"
            tasks.append({
                "task_id": "weather",
                "intent": "information_query",
                "query": f"查询{location}{period}的天气预报",
                "entities": {
                    key: value for key, value in {
                        "destination": destination,
                        "date": date_phrase,
                    }.items() if value
                },
                "depends_on": [],
                "side_effect": False,
                "failure_policy": "continue",
                "display_order": order,
            })
        return tasks

    @staticmethod
    def _extract_destination(query: str) -> str:
        for pattern in _DESTINATION_PATTERNS:
            match = pattern.search(query or "")
            if match:
                return match.group(1).strip()
        return ""

    @staticmethod
    def _extract_date(query: str) -> str:
        match = _DATE_PATTERN.search(query or "")
        return match.group(1) if match else ""
