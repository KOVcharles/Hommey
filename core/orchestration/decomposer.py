"""LLM-assisted task decomposition with a deterministic skill-template fallback."""
from __future__ import annotations

from datetime import datetime
import json
import logging
import re
from typing import Any, Dict, List, Optional

from core.execution_budget import ExecutionLimitExceeded, consume_agent_call
from core.intent_catalog import (
    build_intent_prompt_section,
    execution_steps_for_intent,
)
from core.intent_result import parse_json_object
from core.llm_response import extract_text_from_response

from .graph_builder import TaskGraphBuilder

logger = logging.getLogger(__name__)

_DATE_PATTERN = re.compile(r"(今天|明天|后天|这两天|未来两天|未来几天|本周|下周|\d{1,2}月\d{1,2}日)")
_DESTINATION_PATTERNS = (
    re.compile(r"(?:去|到|前往)([一-鿿]{2,10}?)(?:市)?(?:出差|差旅)"),
    re.compile(
        r"(?:查|查询|看看|了解)?(?:一下)?(?:今天|明天|后天|这两天|未来两天|未来几天)?"
        r"([一-鿿]{2,8}?)(?:市)?(?:的)?天气"
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
        entities = intention_data.get("key_entities") or {}
        if self.model is None:
            return self.fallback(query, intents, entities)

        prompt = self._prompt(query, intents, entities)
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
            return self.fallback(query, intents, entities)

    @staticmethod
    def _prompt(query: str, intents: List[str], entities: Dict[str, Any]) -> str:
        today = datetime.now().astimezone().date().isoformat()
        return f"""【当前日期】{today}
【原始问题】{query}
【已授权意图】{json.dumps(intents, ensure_ascii=False)}
【已识别实体】{json.dumps(entities, ensure_ascii=False)}

为每个已授权意图生成且只生成一个任务。每个 query 只保留该意图负责的内容：
- 把原始问题按意图切分到各自的 query；不得让某个意图的 query 引入其他意图负责的词域。
- 保留用户原本的查询范围；用户只泛问“差旅标准”时不要自行枚举住宿、交通、补贴、报销或审批。
- 不得增加已授权意图之外的任务。
- 相对日期结合当前日期改写清楚；不确定的信息保持原表达，不要猜测。

【意图类型】
{build_intent_prompt_section()}

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
    def fallback(
        cls,
        query: str,
        intents: List[str],
        entities: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Deterministic DAG used when decomposition LLM is unavailable or invalid.

        任务 query 由 skill 执行模板渲染（占位符由 key_entities + 正则补全），
        单步意图直接用语义 query；工作流意图的每步 query 在 graph builder 展开时
        从模板生成，因此这里对全部 intent 一视同仁，不需要按意图分支。
        """
        merged_entities = dict(entities or {})
        destination = cls._extract_destination(query)
        date_phrase = cls._extract_date(query)
        if destination and "destination" not in merged_entities:
            merged_entities["destination"] = destination
        if date_phrase and "start_date" not in merged_entities:
            merged_entities["start_date"] = date_phrase

        tasks = []
        for order, intent in enumerate(intents):
            tasks.append({
                "task_id": intent,
                "intent": intent,
                "query": cls._task_query(intent, merged_entities, query),
                "entities": merged_entities,
                "depends_on": [],
                "side_effect": False,
                "failure_policy": "continue",
                "display_order": order,
            })
        return tasks

    @classmethod
    def _task_query(cls, intent: str, entities: Dict[str, Any], original_query: str) -> str:
        """单步意图渲染首个执行步骤的 scoped query；无模板或工作流则退回原始问题。"""
        steps = execution_steps_for_intent(intent)
        if len(steps) > 1:
            return original_query
        if steps:
            template = steps[0].query
            if template and "{" in template:
                rendered = TaskGraphBuilder._render_query(template, entities)
                if rendered:
                    return rendered
        return original_query

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
