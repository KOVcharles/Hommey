"""Scoped business adapters. No arbitrary MCP, URLs, filesystem or SQL tools."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from uuid import uuid4

from context.memory_repository import stable_uuid
from core.execution_budget import consume_external_call
from core.trip_intake import beijing_today
from utils.io_executor import run_blocking
from utils.memory_safety import sanitize_memory_value
from .contracts import (
    CommuteRequest, MemorySearch, PlaceRequest, Query, SourceRequest,
    ToolRejected, TrainRequest, WeatherRequest, schema,
)


TOOLS = {
    "search_policy": (Query, "检索本部署企业差旅制度，返回来源摘要；使用 read_source 回读后总结"),
    "search_memory": (MemorySearch, "仅检索当前用户的偏好、历史计划和对话；支持关键词过滤"),
    "read_source": (SourceRequest, "回读本任务已检索或明确传入的来源"),
    "search_trains": (TrainRequest, "查询真实车次和余票；日期必须明确，不支持购票"),
    "get_weather": (WeatherRequest, "查询差旅城市天气"),
    "find_hotels": (PlaceRequest, "查询工作地点附近酒店 POI；仅参考消费，不是实时房价"),
    "search_commute": (CommuteRequest, "查询差旅地点之间的公共交通路线"),
}


def tool_schemas(names):
    return [schema(name, TOOLS[name][1], TOOLS[name][0]) for name in names]


def plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


class BusinessServices:
    def __init__(self, memory_manager, *, retriever=None, travel=None, trains=None):
        self.memory = memory_manager
        self.pool = memory_manager.long_term.pool
        self.retriever = retriever
        self.travel = travel
        self.trains = trains

    def _policy(self, query):
        if self.retriever is None:
            from rag.retriever import KnowledgeRetriever
            self.retriever = KnowledgeRetriever()
        if not self.retriever.initialized:
            raise ToolRejected("企业制度知识库暂不可用")
        consume_external_call("rag")
        return self.retriever.search(query, top_k=5)

    def _memory_search(self, scope, request):
        result = []
        if request.kind in {"all", "preferences"}:
            result.append({"kind": "preferences", "data": self.memory.long_term.get_preference()})
        query = request.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = "%" + query + "%"
        with self.pool.connection() as conn, conn.cursor() as cur:
            if request.kind in {"all", "trips"}:
                cur.execute("""SELECT trip_id, record_state, context_data, updated_at
                    FROM supervisor_trip_records WHERE user_id=%s AND context_data::text ILIKE %s
                    ORDER BY updated_at DESC LIMIT %s""", (scope.user_id, pattern, request.limit))
                result.extend({"kind": "trip", "record_state": row["record_state"], "data": dict(row)} for row in cur.fetchall())
                cur.execute("""SELECT trip_id, origin, destination, start_date, end_date, purpose, created_at
                    FROM trip_history WHERE user_id=%s
                    AND concat_ws(' ',origin,destination,purpose,start_date) ILIKE %s
                    ORDER BY created_at DESC LIMIT %s""", (scope.user_id, pattern, request.limit))
                result.extend({"kind": "trip", "record_state": "legacy_unknown", "data": dict(row)} for row in cur.fetchall())
                cur.execute("""SELECT session_id, context_data FROM active_trip_contexts WHERE user_id=%s
                    AND context_data::text ILIKE %s ORDER BY updated_at DESC LIMIT %s""", (scope.user_id, pattern, request.limit))
                result.extend({"kind": "trip", "record_state": "planned", "data": dict(row)} for row in cur.fetchall() if not (row.get("context_data") or {}).get("_trip_id"))
            if request.kind in {"all", "messages"}:
                cur.execute("""SELECT message_id, session_id, role, content, created_at FROM conversation_messages
                    WHERE user_id=%s AND deleted_at IS NULL AND retention_until>NOW() AND content ILIKE %s
                    ORDER BY created_at DESC LIMIT %s""", (scope.user_id, pattern, request.limit))
                result.extend({"kind": "message", "data": dict(row)} for row in cur.fetchall())
        return sanitize_memory_value(json.loads(json.dumps(result[:request.limit], ensure_ascii=False, default=str)))

    async def context(self, scope):
        """Small same-session context; historical search belongs to the memory specialist."""
        def read():
            with self.pool.connection() as conn, conn.cursor() as cur:
                cur.execute("""SELECT role, content, sequence_no FROM conversation_messages
                    WHERE user_id=%s AND session_id=%s AND deleted_at IS NULL AND retention_until>NOW()
                    ORDER BY sequence_no DESC LIMIT 8""", (scope.user_id, stable_uuid(scope.session_id, namespace="session")))
                recent = [{"role": r["role"], "content": r["content"][:1400]} for r in reversed(cur.fetchall())]
                cur.execute("""SELECT summary_text, source_sequence_to FROM session_summaries
                    WHERE user_id=%s AND session_id=%s AND status='done'
                    ORDER BY source_sequence_to DESC LIMIT 2""", (scope.user_id, stable_uuid(scope.session_id, namespace="session")))
                summaries = [{"text": r["summary_text"][:1600], "through_sequence": r["source_sequence_to"]} for r in cur.fetchall()]
            return {"recent": recent, "session_summaries": summaries,
                    "trip": self.memory.get_active_trip() or {},
                    "today": beijing_today()}
        return await run_blocking(read)

    async def execute(self, scope, name, request):
        if scope.user_id != self.memory.user_id:
            raise ToolRejected("身份不匹配")
        if name == "search_policy":
            return "policy", await run_blocking(self._policy, request.query)
        if name == "search_memory":
            return "memory", await run_blocking(self._memory_search, scope, request)
        if name == "search_trains":
            date.fromisoformat(request.date)
            if self.trains is None:
                from core.integrations.trains import create_train_query_backend
                self.trains = create_train_query_backend()
            rows = await self.trains.query_trains(request.origin, request.destination, request.date)
            return "train", [{**row, "travel_date": request.date} for row in rows[:12]]
        if self.travel is None:
            from core.integrations.travel_info import TravelInformationService
            self.travel = TravelInformationService()
        if name == "get_weather":
            value = await self.travel.weather(request.city)
            if value is None:
                raise ToolRejected("未查到该城市天气")
            return "weather", plain(value)
        if name == "find_hotels":
            anchor, candidates = await self.travel.resolve_anchor(request.keyword, city=request.city)
            if anchor is None:
                return "place_candidates", {"needs_input": True, "candidates": plain(candidates)}
            return "hotel", {"anchor": plain(anchor), "hotels": plain(await self.travel.places.nearby_hotels(anchor))}
        if name == "search_commute":
            origin, oc = await self.travel.resolve_anchor(request.origin, city=request.city)
            destination, dc = await self.travel.resolve_anchor(request.destination, city=request.city)
            if not origin or not destination:
                return "place_candidates", {"needs_input": True, "origin": plain(oc), "destination": plain(dc)}
            return "commute", plain(await self.travel.transit_routes(origin, destination))
        raise ToolRejected("不支持的业务工具")


class SourceScope:
    """Fresh per child; references do not grant access to other children's stores."""
    def __init__(self, sources=()):
        self.sources = {s["id"]: dict(s) for s in sources}
        self.read_ids: set[str] = set()

    def add(self, kind, data):
        data = json.loads(json.dumps(data, ensure_ascii=False, default=str))
        source_id = "src_" + uuid4().hex[:16]
        self.sources[source_id] = {"id": source_id, "kind": kind, "data": data,
            "retrieved_at": datetime.now(timezone.utc).isoformat()}
        # Search returns excerpts; full raw source stays in this isolated scope.
        excerpt = json.dumps(data, ensure_ascii=False, default=str)[:2200]
        return {"source_id": source_id, "kind": kind, "excerpt": excerpt, "truncated": len(json.dumps(data, default=str)) > 2200}

    def read(self, source_id):
        if source_id not in self.sources:
            raise ToolRejected("来源不属于本任务，无法读取")
        self.read_ids.add(source_id)
        source = self.sources[source_id]
        encoded = json.dumps(source, ensure_ascii=False, default=str)
        # Never silently cut JSON facts. Fail explicitly if an adapter violates its bound.
        if len(encoded) > 24000:
            raise ToolRejected("来源过大，请缩小检索问题")
        return source
