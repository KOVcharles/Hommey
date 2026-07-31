"""Evidence-bound company trip compliance checker."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, Optional, Union

from agentscope.agent import AgentBase
from agentscope.message import Msg
from pydantic import BaseModel, ConfigDict, Field

from core.llm_response import extract_text_from_response
from core.execution_budget import ExecutionLimitExceeded
from core.intent_result import parse_json_object
from utils.skill_loader import SkillLoader


class ComplianceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: str
    status: Literal["compliant", "non_compliant", "unknown"]
    reason: str = ""
    source_indexes: List[int] = Field(default_factory=list)


class ComplianceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["success", "insufficient_evidence"]
    verdict: Literal["compliant", "non_compliant", "partial", "unknown"]
    checks: List[ComplianceCheck]
    sources: List[Dict[str, Any]]
    unknown_items: List[str]
    missing_info: List[str]
    summary: str


class TripComplianceAgent(AgentBase):
    def __init__(self, name: str = "trip_compliance", model=None, skills_root=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model
        self.skill_loader = SkillLoader(skills_root)

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        payload = self._payload(x)
        trip, answer, sources = self._collect_inputs(payload)
        missing = self._missing_trip_fields(trip)

        if not sources:
            result = {
                "status": "insufficient_evidence",
                "verdict": "unknown",
                "checks": [],
                "sources": [],
                "unknown_items": ["公司差旅制度证据不足，无法确认合规性"],
                "missing_info": missing,
                "summary": "知识库没有提供足够的公司制度证据，本次行程暂时无法确认是否合规。",
            }
            return self._message(result)

        instructions = self.skill_loader.get_skill_content("check-trip-compliance") or ""
        evidence_rules = self.skill_loader.get_skill_resource(
            "check-trip-compliance",
            "references/evidence-rules.md",
        ) or ""
        prompt = f"""你是企业差旅行程合规检查器。严格基于给定证据输出 JSON，不使用常识补全制度。

【出差事项】
{json.dumps(trip, ensure_ascii=False, indent=2)}

【知识库回答】
{answer or '无'}

【知识库来源】
{json.dumps(sources, ensure_ascii=False, indent=2)}

【Skill 指令】
{instructions}

【证据冲突与来源规则】
{evidence_rules}

每个确定结论必须引用 sources 中的来源。证据不足则标记 unknown。只输出 JSON。"""
        try:
            response = await self.model([{"role": "user", "content": prompt}])
            result = parse_json_object(await extract_text_from_response(response))
            result.setdefault("missing_info", missing)
            result.setdefault("sources", sources)
            result = ComplianceOutput.model_validate(result).model_dump()
        except ExecutionLimitExceeded:
            raise
        except Exception:
            result = {
                "status": "insufficient_evidence",
                "verdict": "unknown",
                "checks": [],
                "sources": sources,
                "unknown_items": ["合规检查模型未能生成可靠的结构化结论"],
                "missing_info": missing,
                "summary": "已取得制度来源，但暂时无法形成可靠的合规结论，请人工核验。",
            }
        return self._message(result)

    def _payload(self, x) -> Dict[str, Any]:
        if x is None:
            return {}
        raw = x[-1].content if isinstance(x, list) and x else getattr(x, "content", x)
        if isinstance(raw, dict):
            return raw
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def _collect_inputs(self, payload: Dict[str, Any]):
        trip: Dict[str, Any] = {}
        answer = ""
        sources: List[Dict[str, Any]] = []
        context = payload.get("context") or {}
        active_trip = context.get("active_trip")
        if isinstance(active_trip, dict):
            trip.update(active_trip)

        for previous in payload.get("previous_results") or []:
            agent_name = previous.get("agent_name")
            result = previous.get("result") or {}
            data = result.get("data") if isinstance(result, dict) else {}
            if not isinstance(data, dict):
                continue
            if agent_name == "event_collection":
                trip.update(data.get("data") if isinstance(data.get("data"), dict) else data)
            elif agent_name == "rag_knowledge":
                inner = data.get("data") if isinstance(data.get("data"), dict) else data
                answer = inner.get("answer") or inner.get("content") or ""
                raw_sources = inner.get("retrieved_documents") or inner.get("sources") or []
                sources.extend(self._normalize_sources(raw_sources))
            elif agent_name == "itinerary_planning":
                inner = data.get("data") if isinstance(data.get("data"), dict) else data
                if inner.get("itinerary"):
                    trip["proposed_itinerary"] = inner["itinerary"]
        return trip, answer, sources

    def _normalize_sources(self, raw_sources) -> List[Dict[str, Any]]:
        normalized = []
        for item in raw_sources if isinstance(raw_sources, list) else []:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            normalized.append({
                "file": metadata.get("source") or metadata.get("file_name") or item.get("source") or "企业差旅知识库",
                "page": metadata.get("page") or metadata.get("page_number"),
                "section": metadata.get("section") or metadata.get("title"),
                "excerpt": item.get("content") or item.get("text") or "",
            })
        return normalized

    def _missing_trip_fields(self, trip: Dict[str, Any]) -> List[str]:
        labels = {
            "origin": "出发地",
            "destination": "目的地",
            "start_date": "出发日期",
            "end_date": "返程日期",
        }
        return [label for key, label in labels.items() if not trip.get(key)]

    def _message(self, result: Dict[str, Any]) -> Msg:
        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")
