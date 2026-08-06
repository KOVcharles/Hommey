"""Deterministic AnswerDocument builder used when the Composer LLM fails."""
from __future__ import annotations

from datetime import datetime
import re
from typing import Iterable, List

from core.presentation import (
    AnswerDocument,
    AnswerItem,
    AnswerSection,
    AnswerSource,
    WeatherDay,
    render_plain_text,
)

from .models import IntentTask, TaskResult

_FORECAST_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2})[:：]\s*([^，；;]+?)\s*[，,]\s*"
    r"([^，；;~～]+)\s*[~～]\s*([^，；;°]+)°?C"
    r"(?:[，,]\s*最高降水概率\s*([^；;。]+))?"
)
_CURRENT_PATTERN = re.compile(
    r"当前天气[:：]\s*([^，。]+)[，,]\s*气温\s*([^，。]+)[，,]\s*湿度\s*([^，。]+)"
)


class FallbackComposer:
    def compose(
        self,
        tasks: Iterable[IntentTask],
        results: Iterable[TaskResult],
    ) -> AnswerDocument:
        task_list = list(tasks)
        result_by_task = {result.task_id: result for result in results}
        destination = next(
            (
                str(task.entities.get("destination"))
                for task in task_list
                if task.entities.get("destination")
            ),
            "本次出差",
        )
        sections: List[AnswerSection] = []
        notices = []
        for task in sorted(task_list, key=lambda item: item.display_order):
            result = result_by_task.get(task.task_id)
            if result is None or result.status != "success":
                label = "差旅标准" if task.intent == "rag_knowledge" else "天气信息"
                message = (result.error_message if result else None) or f"{label}暂时查询失败，请稍后重试。"
                sections.append(AnswerSection(
                    kind="policy" if task.intent == "rag_knowledge" else "weather",
                    title=label,
                    status="error",
                    body=message,
                ))
                notices.append(message)
                continue
            if task.intent == "rag_knowledge":
                sections.append(self._policy_section(result))
            elif task.intent == "information_query":
                sections.append(self._weather_section(result))

        summary = self._summary(sections)
        document = AnswerDocument(
            title=f"{destination}差旅信息" if destination != "本次出差" else destination,
            summary=summary,
            sections=sections,
            notices=list(dict.fromkeys(notices)),
            sources=self.sources(results),
        )
        document.plain_text = render_plain_text(document)
        return document

    @staticmethod
    def _policy_section(result: TaskResult) -> AnswerSection:
        data = result.data
        answer = data.get("answer") or data.get("content") or "已取得适用的差旅制度信息。"
        if isinstance(answer, dict):
            answer = answer.get("answer") or str(answer)
        return AnswerSection(kind="policy", title="差旅标准", body=str(answer))

    @staticmethod
    def _weather_section(result: TaskResult) -> AnswerSection:
        payload = result.data.get("results") if isinstance(result.data.get("results"), dict) else result.data
        summary = str(payload.get("summary") or payload.get("message") or "已取得天气信息。")
        items = []
        current = _CURRENT_PATTERN.search(summary)
        if current:
            items.append(AnswerItem(
                label="当前",
                value=f"{current.group(1).strip()} · {current.group(2).strip()}",
                detail=f"湿度 {current.group(3).strip()}",
            ))
        days = [
            WeatherDay(
                date=match.group(1),
                condition=match.group(2).strip(),
                low=f"{match.group(3).strip()}°C",
                high=f"{match.group(4).strip()}°C",
                precipitation=(match.group(5) or "").strip(),
            )
            for match in _FORECAST_PATTERN.finditer(summary)
        ]
        return AnswerSection(
            kind="weather",
            title="目的地天气",
            body="" if items or days else summary,
            items=items,
            days=days,
        )

    @staticmethod
    def _summary(sections: Iterable[AnswerSection]) -> str:
        successful = [section.title for section in sections if section.status == "success"]
        failed = [section.title for section in sections if section.status == "error"]
        if successful and not failed:
            return "已整理好" + "和".join(successful) + "。"
        if successful:
            return "已取得" + "、".join(successful) + "；" + "、".join(failed) + "暂时不可用。"
        return "相关信息暂时未能取得，请稍后重试。"

    @staticmethod
    def sources(results: Iterable[TaskResult]) -> List[AnswerSource]:
        sources = []
        seen = set()
        weather_time = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        for result in results:
            for source in result.evidence:
                title = str(
                    source.get("title") or source.get("file") or source.get("source") or "数据来源"
                )
                detail = " · ".join(
                    str(value) for value in (
                        source.get("section"),
                        source.get("page") and f"第{source['page']}页",
                    ) if value
                )
                url = str(source.get("url") or "")
                key = (title, detail, url)
                if key in seen:
                    continue
                seen.add(key)
                sources.append(AnswerSource(
                    title=title,
                    detail=detail,
                    url=url,
                    updated_at=weather_time if result.intent == "information_query" else "",
                ))
        return sources
