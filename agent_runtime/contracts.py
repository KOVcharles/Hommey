"""Small, transport-independent contracts. Identity never comes from the model."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    kind: Literal["answer", "help", "ask", "clarify", "refuse"] = Field(default="answer",
        description="answer 展示所选业务报告；help 展示服务介绍＋所选业务报告，仅介绍时不需要报告；ask 已确定任务但缺资料；clarify 意图不明，不启动业务；refuse 明确超范围")
    result_ids: list[str] = Field(default_factory=list, max_length=16,
        description="需要在本轮完整展示的业务报告，不是参考资料列表。仅介绍助手时留空；用户同时要求业务结论时在此选择报告，读取不会自动展示。")
    question: str = Field(default="", max_length=500)
    reuse_reasons: dict[str, str] = Field(default_factory=dict, max_length=16,
        description="交付历史结果时必填：每个历史 result_id 对应其与本轮问题的具体关联；本轮结果不填。读取历史不等于交付历史，不为凑齐答案附带无关旧报告。")


class ReadResult(StrictModel):
    result_id: str


class ReadSkill(StrictModel):
    name: Literal["event-collection", "ask-question", "memory-query", "preference",
                  "query-info", "train-query", "place-query", "plan-trip", "check-trip-compliance"]
    resource: str = Field(default="", max_length=160, description="留空读取入口；仅可使用返回的 available_resources 路径，不是工具名或其他 Skill 名")


class ApplyChanges(StrictModel):
    result_id: str


class Report(StrictModel):
    status: Literal["success", "partial", "needs_input", "unavailable", "error"] = "success"
    summary: str = Field(min_length=1, max_length=2400)
    data: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list, max_length=24)
    missing_info: list[str] = Field(default_factory=list, max_length=12)


class PolicyFinding(StrictModel):
    item: str = Field(min_length=1, max_length=60)
    conclusion: str = Field(min_length=1, max_length=300, description="直接回答用户的具体标准，包含金额、币种、单位或规则；不要只说已查到")
    applicability: str = Field(default="", max_length=300, description="适用机构、经费来源、人员类型、城市、有效期及例外；未知条件明确说明")
    evidence_refs: list[str] = Field(max_length=6, description="必填：确定结论的直接来源；没有来源的未知事项填空数组，不会作为标准展示")


class PolicyData(StrictModel):
    findings: list[PolicyFinding] = Field(max_length=12, description="逐项列出已核实的具体标准及来源，资料不可用时才留空数组")


class PolicyReport(Report):
    summary: str = Field(min_length=1, max_length=240, description="一句话给出结论和关键未知项，不重复 findings")
    data: PolicyData
    evidence_refs: list[str] = Field(max_length=24)


class TripFields(StrictModel):
    origin: str | None = None
    destination: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    duration_days: int | None = Field(default=None, strict=True, ge=1, le=90)
    trip_purpose: str | None = None
    work_location: str | None = None
    work_schedule: str | None = None


class TripData(StrictModel):
    trip: TripFields
    field_sources: dict[str, str] = Field(description="每个变更字段对应的本轮用户原文，不得改写")
    trip_action: Literal["update", "new", "cancel"] = "update"
    action_source: str = ""
    constraints: list[str] = Field(default_factory=list, max_length=12)


class TripReport(Report):
    data: TripData


class EvidenceReport(Report):
    evidence_refs: list[str] = Field(max_length=24, description="必填：已回读且直接支持结论的来源 ID；没有证据填空数组并返回 unavailable")


class Preferences(StrictModel):
    home_location: str | None = None
    transportation_preference: str | None = None
    seat_preference: str | None = None
    meal_preference: str | None = None
    budget_level: str | None = None
    hotel_brands: list[str] | None = None
    airlines: list[str] | None = None


class MemoryData(StrictModel):
    findings: list[PolicyFinding] = Field(default_factory=list, max_length=6, description="最多六项已核实的记忆事实；未知项放 missing_info，不穷举所有偏好字段")
    preferences: Preferences = Field(default_factory=Preferences)
    preference_sources: dict[str, str] = Field(default_factory=dict)


class MemoryReport(EvidenceReport):
    data: MemoryData


class TravelData(StrictModel):
    findings: list[PolicyFinding] = Field(max_length=6, description="提取交通、天气、酒店或通勤事实，每项引用已读来源；不能只说已查询")


class TravelReport(EvidenceReport):
    data: TravelData


class ItineraryDay(StrictModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    activities: list[str] = Field(min_length=1, max_length=12)


class Itinerary(StrictModel):
    days: list[ItineraryDay] = Field(max_length=30)


class PlanData(StrictModel):
    itinerary: Itinerary
    decision_basis: list[str] = Field(default_factory=list, max_length=12)


class PlanReport(Report):
    data: PlanData

    @model_validator(mode="after")
    def successful_plan_has_days(self):
        if self.status == "success" and not self.data.itinerary.days:
            raise ValueError("成功规划必须包含每日安排；日期未知时返回 needs_input 或 partial")
        return self


class ComplianceCheck(StrictModel):
    item: str
    status: Literal["compliant", "non_compliant", "unknown"]
    reason: str
    evidence_refs: list[str] = Field(max_length=6)


class ComplianceData(StrictModel):
    verdict: Literal["compliant", "non_compliant", "partial", "unknown"]
    checks: list[ComplianceCheck] = Field(default_factory=list, max_length=12)


class ComplianceReport(EvidenceReport):
    data: ComplianceData


REPORT_MODELS = {"trip_context": TripReport, "policy_rag": PolicyReport,
                 "memory": MemoryReport, "travel_info": TravelReport,
                 "trip_planner": PlanReport, "compliance": ComplianceReport}


class WorkItem(StrictModel):
    role: Role
    task: str
    input_version: int
    dependency_ids: list[str] = Field(default_factory=list)
    status: Literal["pending", "running", "completed", "partial", "needs_input", "failed", "skipped", "cancelled"] = "pending"
    attempts: int = 1
    result_id: str
    error_code: str | None = None
    step_id: str | None = None
    title: str | None = Field(default=None, max_length=100)
    purpose: str | None = Field(default=None, max_length=200)
    summary: str = Field(default="", max_length=300)
    started_at: str | None = None
    finished_at: str | None = None


class ToolFailure(StrictModel):
    code: str
    message: str
    retryable: bool = False
    next_action: Literal["repair_arguments", "report", "finish", "apply_changes", "ask_user"] = "repair_arguments"
    fields: list[str] = Field(default_factory=list)
    violations: list[dict[str, str]] = Field(default_factory=list)

    def wire(self):
        return {"error": self.message, "failure": self.model_dump()}


class SpecialistResult(Report):
    result_id: str
    role: Role
    task: str
    input_version: int = 0
    sources: list[dict[str, Any]] = Field(default_factory=list)

    def brief(self) -> dict[str, Any]:
        # Retrieval reports already enforce a 4k data budget. Return their
        # extracted facts with the summary, never raw source documents/logs.
        value = self.model_dump(include={"result_id", "role", "status", "summary", "evidence_refs", "missing_info", "data"})
        if self.role not in {"policy_rag", "memory", "travel_info"}:
            value.pop("data", None)
        for key in ("data", "missing_info", "evidence_refs"):
            if not value.get(key):
                value.pop(key, None)
        citations = []
        for source in self.sources:
            if source["id"] not in self.evidence_refs:
                continue
            metadata = source.get("data", {}).get("metadata", {}) if isinstance(source.get("data"), dict) else {}
            citation = {"source_id": source["id"], **{k: metadata[k] for k in
                ("title", "section", "page", "document_version", "effective_date") if metadata.get(k) is not None}}
            if source["kind"] != "policy" and source.get("retrieved_at"):
                citation["retrieved_at"] = source["retrieved_at"]
            citations.append(citation)
        if citations:
            value["citations"] = citations
        return value


class Query(StrictModel):
    query: str = Field(min_length=1, max_length=500)


class SourceRequest(StrictModel):
    source_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4000, ge=500, le=6000)


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
    use_trip_location: bool = Field(default=False, description="查询当前已确认工作地点附近酒店时设为 true；由宿主使用已验证地点，避免重新搜索地点")


class CommuteRequest(StrictModel):
    city: str = Field(min_length=1, max_length=80)
    origin: str = Field(min_length=1, max_length=160)
    destination: str = Field(min_length=1, max_length=160)


class Empty(StrictModel):
    pass


def schema(name: str, description: str, model: type[BaseModel]) -> dict:
    parameters = model.model_json_schema()
    definitions = parameters.pop("$defs", {})

    def wire_schema(value):
        if isinstance(value, list):
            return [wire_schema(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            value = {**definitions[value["$ref"].rsplit("/", 1)[-1]], **{k: v for k, v in value.items() if k != "$ref"}}
        # Compatible providers vary in support for nested references and length
        # constraints. Keep explicit nested types on the wire; Pydantic remains
        # the authoritative validator for every bound, regardless of provider.
        return {key: wire_schema(item) for key, item in value.items()
                if key not in {"title", "maxLength", "minLength", "maxItems", "minItems"}}

    return {"type": "function", "function": {
        "name": name, "description": description, "parameters": wire_schema(parameters),
    }}


class RuntimeStopped(RuntimeError):
    """Cancellation, ownership loss or execution budget exhaustion."""


class ToolRejected(ValueError):
    """A recoverable, safe-to-show tool validation error."""

    def __init__(self, message, *, code="INVALID_OPERATION", next_action="repair_arguments", fields=None):
        super().__init__(message)
        self.failure = ToolFailure(code=code, message=message, next_action=next_action, fields=fields or [])
