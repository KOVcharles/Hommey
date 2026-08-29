"""Internal deterministic place-information execution agent."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional, Union

from agentscope.agent import AgentBase
from agentscope.message import Msg

from core.integrations.places.amap import AMapError
from core.integrations.places.models import VerifiedPlace
from core.integrations.places.service import PlaceInformationService


class PlaceInformationAgent(AgentBase):
    def __init__(
        self, name: str = "PlaceInformationAgent", model=None,
        service: PlaceInformationService | None = None, **kwargs,
    ):
        super().__init__()
        self.name = name
        self.model = model
        self.service = service or PlaceInformationService()

    async def reply(self, x: Optional[Union[Msg, list[Msg]]] = None) -> Msg:
        payload = self._payload(x)
        context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
        active_task = context.get("active_task") if isinstance(context.get("active_task"), dict) else {}
        query = str(active_task.get("query") or context.get("agent_query") or "").strip()
        entities = active_task.get("entities") if isinstance(active_task.get("entities"), dict) else {}
        capabilities = set(active_task.get("capabilities") or [])
        trip = self._trip(payload.get("previous_results") or [])

        city = str(
            trip.get("destination") or entities.get("destination") or ""
        ).strip()
        work_location = str(
            trip.get("work_location")
            or entities.get("work_location")
            or self._extract_anchor(query)
            or ""
        ).strip()
        verified_input = trip.get("work_location_verified") or entities.get("work_location_verified")
        try:
            verified_anchor = (
                VerifiedPlace.model_validate(verified_input)
                if isinstance(verified_input, dict) else None
            )
        except ValueError:
            verified_anchor = None

        if not self.service.configured:
            return self._message({
                "query_success": False,
                "results": {"message": "地点服务尚未配置，暂时无法查询真实地点和附近酒店。"},
            })
        if not work_location:
            return self._message({
                "query_success": True,
                "skipped": True,
                "results": {
                    "message": "未提供明确的会议或客户地点，未按城市中心猜测附近酒店。",
                    "hotels": [],
                },
            })

        try:
            if verified_anchor is not None:
                anchor, candidates = verified_anchor, [verified_anchor]
            else:
                anchor, candidates = await self.service.resolve_anchor(work_location, city=city)
            if anchor is None:
                return self._message({
                    "query_success": True,
                    "needs_confirmation": bool(candidates),
                    "results": {
                        "message": (
                            "找到多个可能的地点，请确认具体地点后再查询附近酒店。"
                            if candidates else "没有找到可核验的地点，请补充城市或更完整的地址。"
                        ),
                        "place_candidates": [
                            item.model_dump(mode="json") for item in candidates[:5]
                        ],
                        "hotels": [],
                    },
                    "sources": [self._source()] if candidates else [],
                })
            hotels = []
            if not capabilities or "nearby_hotels" in capabilities:
                hotels = await self.service.nearby_hotels(anchor, limit=3)
            return self._message({
                "query_success": True,
                "results": {
                    "anchor": anchor.model_dump(mode="json"),
                    "hotels": [item.model_dump(mode="json") for item in hotels],
                    "summary": self._summary(anchor.name, hotels),
                    "price_notice": "高德参考消费不是指定日期实时房价或可订库存。",
                },
                "sources": [self._source(anchor.provider_place_id)],
            })
        except AMapError as exc:
            return self._message({
                "query_success": False,
                "results": {"message": str(exc)},
            })

    def _message(self, content: dict[str, Any]) -> Msg:
        return Msg(name=self.name, content=json.dumps(content, ensure_ascii=False), role="assistant")

    @staticmethod
    def _payload(x) -> dict[str, Any]:
        if x is None:
            return {}
        content = x[-1].content if isinstance(x, list) and x else getattr(x, "content", x)
        if isinstance(content, dict):
            return content
        try:
            parsed = json.loads(str(content))
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _trip(previous_results: list[dict[str, Any]]) -> dict[str, Any]:
        for item in reversed(previous_results):
            if not isinstance(item, dict) or item.get("agent_name") != "event_collection":
                continue
            data = (item.get("result") or {}).get("data") or {}
            return data if isinstance(data, dict) else {}
        return {}

    @staticmethod
    def _extract_anchor(query: str) -> str:
        text = re.sub(r"[？?！!。]", "", str(query or "")).strip()
        if any(marker in text for marker in (
            "本次出差的工作地点", "当前问题中的地点", "明确的会议或客户地点",
        )):
            return ""
        match = re.search(r"(.{2,60}?)(?:附近|周边)(?:的|有|有哪些|有什么)?(?:酒店|住宿)", text)
        if match:
            value = match.group(1)
        else:
            match = re.search(r"(?:查询|搜索|查找|找|推荐)?(.{2,60}?)(?:酒店|住宿)", text)
            value = match.group(1) if match else ""
        value = re.sub(
            r"^(?:帮我|请|查询|搜索|查一下|查找|推荐|我想找|我要找|出差去|去)",
            "", value,
        )
        return value.strip(" ，,的")

    @staticmethod
    def _source(anchor_id: str = "") -> dict[str, Any]:
        return {
            "provider": "amap",
            "title": "高德地图地点搜索",
            "anchor_place_id": anchor_id,
            "retrieved_at": datetime.now().astimezone().isoformat(),
        }

    @staticmethod
    def _summary(anchor_name: str, hotels: list[Any]) -> str:
        if not hotels:
            return f"高德暂未返回{anchor_name}附近的有效酒店结果。"
        parts = []
        for hotel in hotels:
            price = (
                f"高德参考消费约{hotel.reference_cost.amount:g}元"
                if hotel.reference_cost else "价格待确认"
            )
            rating = f"，评分{hotel.rating:g}" if hotel.rating is not None else ""
            parts.append(f"{hotel.name}（约{hotel.distance_m}米{rating}，{price}）")
        return f"{anchor_name}附近前三家候选：" + "；".join(parts)
