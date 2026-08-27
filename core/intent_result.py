"""Canonical intent-analysis contracts and rolling-upgrade adapters."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class IntentGroup(BaseModel):
    """One user-facing semantic goal with an isolated, self-contained query."""

    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    intent: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)
    entities: Dict[str, Any] = Field(default_factory=dict)
    source_refs: List[str] = Field(default_factory=list)


class IntentRelation(BaseModel):
    """Semantic relation between groups; it never names an Agent or Skill."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    sources: List[str] = Field(default_factory=list, alias="from")
    target: str = Field(alias="to")
    type: Literal["required_context", "sequence", "compare"]


class IntentAnalysis(BaseModel):
    """The only contract produced by IntentionAgent."""

    model_config = ConfigDict(extra="ignore")

    schema_version: Literal[1] = 1
    groups: List[IntentGroup] = Field(default_factory=list)
    relations: List[IntentRelation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_group_references(self):
        group_ids = [group.group_id for group in self.groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("intent group_id values must be unique")
        known = set(group_ids)
        for relation in self.relations:
            referenced = set(relation.sources) | {relation.target}
            missing = referenced - known
            if missing:
                raise ValueError(f"intent relation references unknown groups: {sorted(missing)}")
            if relation.target in relation.sources:
                raise ValueError("intent relation cannot depend on itself")
        return self


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


class IntentResult(BaseModel):
    # Ignore stale model fields during rolling upgrades, but do not propagate
    # them into the execution contract. In particular, intent recognition no
    # longer produces an executable schedule.
    model_config = ConfigDict(extra="ignore")

    routing: Routing
    reasoning: str = ""
    intents: List[IntentItem] = Field(default_factory=list)
    key_entities: Dict[str, Any] = Field(default_factory=dict)
    rewritten_query: str = ""
    clarification: str = ""


def _safe_group_id(value: str, index: int, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "_", (value or "intent").lower()).strip("_-")
    if not base or not base[0].isalpha():
        base = f"intent_{base}" if base else "intent"
    base = base[:54]
    candidate = base if len(base) >= 3 else f"{base}_goal"
    if candidate in used:
        candidate = f"{candidate[:54]}_{index + 1}"
    used.add(candidate)
    return candidate


def legacy_to_intent_analysis(data: dict, original_query: str = "") -> IntentAnalysis:
    """Convert the v1 routing envelope into canonical semantic groups.

    Authorization flags are deliberately discarded. They are recomputed by
    OrchestrationPolicy after recognition.
    """
    raw_intents = list(data.get("intents") or [])
    if not raw_intents:
        routing = data.get("routing") or {}
        if routing.get("intent"):
            raw_intents = [{
                "type": routing.get("intent"),
                "confidence": routing.get("confidence", 0.0),
            }]

    default_query = str(data.get("rewritten_query") or original_query or "").strip()
    default_entities = data.get("key_entities") if isinstance(data.get("key_entities"), dict) else {}
    groups = []
    used: set[str] = set()
    for index, item in enumerate(raw_intents):
        intent = str(item.get("type") or item.get("intent") or "unclear")
        query = str(item.get("query") or default_query or original_query or intent).strip()
        entities = item.get("entities") if isinstance(item.get("entities"), dict) else default_entities
        source_refs = item.get("source_refs") if isinstance(item.get("source_refs"), list) else []
        groups.append(IntentGroup(
            group_id=_safe_group_id(str(item.get("group_id") or intent), index, used),
            intent=intent,
            query=query,
            confidence=float(item.get("confidence") or 0.0),
            entities=dict(entities or {}),
            source_refs=[str(ref) for ref in source_refs],
        ))
    return IntentAnalysis(groups=groups)


def coerce_intent_analysis(data: dict, original_query: str = "") -> IntentAnalysis:
    """Read canonical output first and legacy output during rolling upgrades."""
    if isinstance(data, dict) and isinstance(data.get("groups"), list):
        try:
            return IntentAnalysis.model_validate(data)
        except ValidationError as exc:
            raise ValueError(f"Invalid intent analysis schema: {exc}") from exc
    return legacy_to_intent_analysis(data or {}, original_query)


def validate_intent_analysis(data: dict, original_query: str = "") -> dict:
    return coerce_intent_analysis(data, original_query).model_dump(by_alias=True)


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
