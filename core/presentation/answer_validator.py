"""Grounding and coverage checks for LLM-composed answer documents."""
from __future__ import annotations

import json
import re
from typing import Iterable

from core.intent_catalog import require_section_for_intent, section_kind_for_intent
from core.orchestration.models import TaskResult

from .answer_document import AnswerDocument

_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")


class AnswerDocumentValidator:
    def validate(self, document: AnswerDocument, results: Iterable[TaskResult]) -> None:
        result_list = list(results)
        kinds = {section.kind for section in document.sections}
        goal_kinds = {
            (section.goal_id, section.kind)
            for section in document.sections if section.goal_id
        }
        for result in result_list:
            if not require_section_for_intent(result.intent):
                continue
            expected = section_kind_for_intent(result.intent)
            if expected and expected not in kinds:
                raise ValueError(f"answer omitted task section: {result.task_id}")
            if result.goal_id and (result.goal_id, expected) not in goal_kinds:
                raise ValueError(f"answer omitted goal coverage: {result.goal_id}")

        source_text = json.dumps(
            [
                {
                    "status": result.status,
                    "data": result.data,
                    "error_message": result.error_message,
                }
                for result in result_list
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        composed_text = "\n".join([
            document.title,
            document.summary,
            *[
                "\n".join([
                    section.title,
                    section.body,
                    *[f"{item.label} {item.value} {item.detail}" for item in section.items],
                    *[
                        f"{day.date} {day.condition} {day.low} {day.high} {day.precipitation}"
                        for day in section.days
                    ],
                ])
                for section in document.sections
            ],
            *document.notices,
        ])
        unsupported = [
            token for token in _NUMBER_PATTERN.findall(composed_text)
            if token not in source_text
        ]
        if unsupported:
            raise ValueError(f"answer introduced unsupported numeric facts: {unsupported[:5]}")
