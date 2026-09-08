"""Small, transport-independent contracts. Identity never comes from the model."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


Role = Literal["trip_context", "policy_rag", "memory", "travel_info", "trip_planner", "compliance"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(StrictModel):
    user_id: str
    session_id: str
    request_id: str
    enterprise_id: str = "default"


class Delegate(StrictModel):
    role: Role
    task: str = Field(min_length=1, max_length=1800)
    result_ids: list[str] = Field(default_factory=list, max_length=12)


class Finish(StrictModel):
    kind: Literal["answer", "ask", "refuse"] = "answer"
    result_ids: list[str] = Field(default_factory=list, max_length=16)
    question: str = Field(default="", max_length=500)


class ReadResult(StrictModel):
    result_id: str


class ReadSkill(StrictModel):
    name: str = Field(min_length=1, max_length=80)


class ApplyChanges(StrictModel):
    result_id: str


class Report(StrictModel):
    status: Literal["success", "partial", "needs_input", "unavailable", "error"] = "success"
    summary: str = Field(min_length=1, max_length=2400)
    data: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list, max_length=24)
    missing_info: list[str] = Field(default_factory=list, max_length=12)


class SpecialistResult(Report):
    result_id: str
    role: Role
    task: str
    input_version: int = 0
    sources: list[dict[str, Any]] = Field(default_factory=list)

    def brief(self) -> dict[str, Any]:
        return self.model_dump(exclude={"data", "sources"})


class Query(StrictModel):
    query: str = Field(min_length=1, max_length=500)


class SourceRequest(StrictModel):
    source_id: str


class MemorySearch(StrictModel):
    query: str = Field(default="", max_length=160)
    kind: Literal["all", "trips", "messages", "preferences"] = "all"
    limit: int = Field(default=8, ge=1, le=20)


class WeatherRequest(StrictModel):
    city: str = Field(min_length=1, max_length=80)


class TrainRequest(StrictModel):
    origin: str = Field(min_length=1, max_length=80)
    destination: str = Field(min_length=1, max_length=80)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class PlaceRequest(StrictModel):
    city: str = Field(min_length=1, max_length=80)
    keyword: str = Field(min_length=1, max_length=160)


class CommuteRequest(StrictModel):
    city: str = Field(min_length=1, max_length=80)
    origin: str = Field(min_length=1, max_length=160)
    destination: str = Field(min_length=1, max_length=160)


class Empty(StrictModel):
    pass


def schema(name: str, description: str, model: type[BaseModel]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description, "parameters": model.model_json_schema(),
    }}


class RuntimeStopped(RuntimeError):
    """Cancellation, ownership loss or execution budget exhaustion."""


class ToolRejected(ValueError):
    """A recoverable, safe-to-show tool validation error."""
