"""Grounded LLM answer composition with deterministic fallback."""
from __future__ import annotations

import json
import logging
from typing import Iterable

from core.execution_budget import consume_agent_call
from core.intent_catalog import section_kind_for_intent
from core.intent_result import parse_json_object
from core.llm_response import extract_text_from_response
from core.presentation import AnswerDocument, render_plain_text
from core.presentation.answer_validator import AnswerDocumentValidator

from .fallback_composer import FallbackComposer
from .models import IntentTask, TaskResult

logger = logging.getLogger(__name__)

_ALLOWED_KINDS = {
    "policy", "weather", "memory", "preference", "trip", "notice", "general",
}


class AnswerComposer:
    def __init__(self, model=None):
        self.model = model
        self.fallback = FallbackComposer()
        self.validator = AnswerDocumentValidator()

    async def compose(
        self,
        original_query: str,
        tasks: Iterable[IntentTask],
        results: Iterable[TaskResult],
    ) -> AnswerDocument:
        task_list = list(tasks)
        result_list = list(results)
        # Trip plans already have a rich, typed itinerary contract. Letting a
        # second LLM redesign that contract made identical trips alternate
        # between a two-column grid and a timeline, depending on whether its
        # JSON happened to validate. Render structured trips deterministically;
        # the model remains responsible for the itinerary facts, not UI shape.
        if self.model is None or any(
            section_kind_for_intent(task.intent) == "trip" for task in task_list
        ):
            return self.fallback.compose(task_list, result_list)
        try:
            consume_agent_call("AnswerComposer")
            response = await self.model([
                {
                    "role": "system",
                    "content": (
                        "你是公司商旅助手的答案编辑器。只整理提供的任务结果，"
                        "不得补充、推测或修改任何金额、日期、温度、比例和制度结论。只输出JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": self._prompt(original_query, task_list, result_list),
                },
            ])
            text = await extract_text_from_response(response)
            payload = parse_json_object(text)
            payload.pop("sources", None)
            payload.pop("plain_text", None)
            payload = self._normalize_optional_fields(payload)
            document = AnswerDocument.model_validate({
                **payload,
                "sources": [
                    source.model_dump()
                    for source in self.fallback.sources(result_list)
                ],
                "plain_text": "",
            })
            self.validator.validate(document, result_list)
            document.plain_text = render_plain_text(document)
            return document
        except Exception as exc:
            logger.warning("Answer composition failed; using deterministic fallback: %s", exc)
            return self.fallback.compose(task_list, result_list)

    @staticmethod
    def _normalize_optional_fields(payload: dict) -> dict:
        """Normalize nullable LLM fields while keeping the public schema stable."""
        normalized = dict(payload)
        normalized["summary"] = normalized.get("summary") or ""
        normalized["notices"] = normalized.get("notices") or []
        sections = []
        for raw_section in normalized.get("sections") or []:
            section = dict(raw_section)
            section["goal_id"] = section.get("goal_id") or ""
            section["body"] = section.get("body") or ""
            section["items"] = section.get("items") or []
            section["days"] = section.get("days") or []
            section["items"] = [
                {**item, "detail": item.get("detail") or ""}
                for item in section["items"]
                if isinstance(item, dict)
            ]
            section["days"] = [
                {
                    **day,
                    "condition": day.get("condition") or "",
                    "low": day.get("low") or "",
                    "high": day.get("high") or "",
                    "precipitation": day.get("precipitation") or "",
                }
                for day in section["days"]
                if isinstance(day, dict)
            ]
            sections.append(section)
        normalized["sections"] = sections
        return normalized

    @staticmethod
    def _prompt(original_query, tasks, results) -> str:
        compact_results = []
        for result in results:
            data = result.data
            if isinstance(data.get("results"), dict):
                facts = data["results"]
            else:
                facts = data
            compact_results.append({
                "task_id": result.task_id,
                "goal_id": result.goal_id,
                "intent": result.intent,
                "agent": result.agent_name,
                "kind": section_kind_for_intent(result.intent),
                "status": result.status,
                "facts": facts,
                "error_message": result.error_message,
            })
        return f"""【用户原始问题】
{original_query}

【任务】
{json.dumps([task.model_dump(mode='json') for task in tasks], ensure_ascii=False)}

【可信任务结果】
{json.dumps(compact_results, ensure_ascii=False)}

生成一张紧凑的统一答案卡片。要求：
- 按用户提问顺序组织 section，不提 Agent、RAG、编排或知识库缺少天气。
- 每个任务 Goal 至少有一个 section，并原样填写该任务的 task_id 到 goal_id；不得把不同 Goal 合并后遗漏。
- 每个意图的 section.kind 用结果中标注的 kind（policy/weather/memory/preference/trip/notice/general）。
- 尽量使用 items 和 days，避免大段文字。
- 失败任务保留对应 section，status=error，并给出一句简短提示。
- summary 不超过80个中文字符。
- 不输出 sources 和 plain_text，它们由系统生成。

JSON结构：
{{
  "version": "1.0",
  "title": "目的地差旅信息",
  "summary": "一句综合结论",
  "sections": [
    {{
      "goal_id": "对应任务的task_id",
      "kind": "policy或weather或memory或preference或trip或notice或general",
      "title": "分区标题",
      "status": "success、partial或error",
      "body": "必要时使用的短文本",
      "items": [{{"label": "标签", "value": "值", "detail": "可选说明"}}],
      "days": [{{"date": "日期", "condition": "天气", "low": "最低温", "high": "最高温", "precipitation": "降水信息"}}]
    }}
  ],
  "notices": []
}}"""
