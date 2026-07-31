"""Intent LLM result parsing and validation."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Routing(BaseModel):
    intent: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""
    should_call_skill: bool = False


class IntentItem(BaseModel):
    type: str
    confidence: float = Field(ge=0.0, le=1.0)
    description: str = ""
    reason: str = ""
    should_call_skill: bool = False


class AgentScheduleItem(BaseModel):
    agent_name: str
    priority: int
    reason: str = ""
    expected_output: str = ""
    on_failure: Literal["abort", "continue"] = "abort"
    max_retries: int = Field(default=0, ge=0, le=2)


class IntentResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    routing: Routing
    reasoning: str = ""
    intents: List[IntentItem] = Field(default_factory=list)
    key_entities: Dict[str, Any] = Field(default_factory=dict)
    rewritten_query: str = ""
    agent_schedule: List[AgentScheduleItem] = Field(default_factory=list)
    clarification: str = ""


def clean_json_text(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def parse_json_object(text: str) -> dict:
    cleaned = clean_json_text(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as first_error:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or start >= end:
            sample = cleaned[:300]
            raise ValueError(f"No JSON object found in model response: {sample}") from first_error

        snippet = cleaned[start:end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError as second_error:
            sample = snippet[:300]
            raise ValueError(f"Failed to parse JSON object: {second_error}. Sample: {sample}") from second_error


def validate_intent_result(data: dict) -> dict:
    try:
        return IntentResult.model_validate(data).model_dump()
    except ValidationError as exc:
        raise ValueError(f"Invalid intent result schema: {exc}") from exc
