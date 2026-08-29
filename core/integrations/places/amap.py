"""Minimal server-side adapter for AMap Web Service APIs."""
from __future__ import annotations

from datetime import datetime
import asyncio
import copy
import json
import logging
import time
from typing import Any

import httpx

from core.execution_budget import consume_external_call
from settings import AMAP_CONFIG
from utils.logging_safety import sanitize_for_log

from .models import GeoPoint, HotelCandidate, ReferenceCost, VerifiedPlace

logger = logging.getLogger(__name__)


class AMapError(RuntimeError):
    """Sanitized provider failure safe to map to a public capability error."""


class AMapProvider:
    def __init__(self, config: dict[str, Any] | None = None, client=None):
        self.config = dict(AMAP_CONFIG if config is None else config)
        self.client = client
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.config.get("enabled") and self.config.get("api_key"))

    async def search_places(
        self, keyword: str, *, city: str = "", limit: int = 5,
    ) -> list[VerifiedPlace]:
        keyword = str(keyword or "").strip()[:80]
        if not keyword:
            return []
        params: dict[str, Any] = {
            "keywords": keyword,
            "offset": max(1, min(int(limit), 20)),
            "page": 1,
            "extensions": "all",
        }
        if city:
            params.update({"city": str(city)[:80], "citylimit": "true"})
        payload = await self._request("/v3/place/text", params)
        return [place for item in payload.get("pois") or [] if (place := self._place(item))]

    async def verify_place_id(self, place_id: str) -> VerifiedPlace | None:
        clean_id = str(place_id or "").strip()[:80]
        if not clean_id:
            return None
        payload = await self._request("/v3/place/detail", {"id": clean_id, "extensions": "all"})
        for item in payload.get("pois") or []:
            place = self._place(item)
            if place and place.provider_place_id == clean_id:
                return place
        return None

    async def nearby_hotels(
        self, anchor: VerifiedPlace, *, limit: int = 3,
    ) -> list[HotelCandidate]:
        point = anchor.location
        payload = await self._request("/v3/place/around", {
            "location": f"{point.lng:.6f},{point.lat:.6f}",
            "keywords": "酒店",
            "types": "100000",
            "radius": int(self.config.get("hotel_radius_m", 5000)),
            "sortrule": "distance",
            "offset": 20,
            "page": 1,
            "extensions": "all",
        })
        retrieved_at = datetime.now().astimezone()
        hotels = [
            hotel for item in payload.get("pois") or []
            if (hotel := self._hotel(item, retrieved_at)) is not None
        ]
        hotels.sort(key=lambda item: (
            item.distance_m,
            -(item.rating if item.rating is not None else -1),
            0 if item.reference_cost is not None else 1,
            item.provider_place_id,
        ))
        return hotels[:max(1, min(int(limit), 3))]

    async def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise AMapError("高德地点服务尚未配置")
        cache_key = json.dumps([path, params], ensure_ascii=False, sort_keys=True, default=str)
        cached = self._cache.get(cache_key)
        now = time.monotonic()
        if cached and cached[0] > now:
            return copy.deepcopy(cached[1])

        safe_params = {**params, "key": self.config["api_key"], "output": "JSON"}
        retries = max(0, min(int(self.config.get("max_retries", 1)), 1))
        last_error: Exception | None = None
        payload: dict[str, Any] | None = None
        for attempt in range(retries + 1):
            consume_external_call("amap")
            try:
                if self.client is not None:
                    response = await self.client.get(path, params=safe_params)
                else:
                    timeout = httpx.Timeout(float(self.config.get("timeout_sec", 8.0)))
                    async with httpx.AsyncClient(
                        base_url=str(self.config.get("base_url") or "https://restapi.amap.com"),
                        timeout=timeout,
                    ) as client:
                        response = await client.get(path, params=safe_params)
                response.raise_for_status()
                payload = response.json()
                break
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code < 500 or attempt >= retries:
                    break
            except (httpx.RequestError, ValueError) as exc:
                last_error = exc
                if attempt >= retries:
                    break
            await asyncio.sleep(0.15 * (attempt + 1))
        if payload is None:
            logger.warning("amap_request_failed path=%s error=%s", path, sanitize_for_log(last_error))
            raise AMapError("高德地点服务暂时不可用") from last_error
        if not isinstance(payload, dict) or str(payload.get("status")) != "1":
            info_code = str(payload.get("infocode") or "UNKNOWN") if isinstance(payload, dict) else "MALFORMED"
            logger.warning("amap_provider_error path=%s infocode=%s", path, info_code[:32])
            raise AMapError("高德地点服务返回失败")
        ttl = max(0, int(self.config.get("cache_ttl_sec", 300)))
        if ttl:
            self._cache[cache_key] = (now + ttl, copy.deepcopy(payload))
            if len(self._cache) > 512:
                self._cache = {
                    key: value for key, value in self._cache.items() if value[0] > now
                }
        return payload

    @staticmethod
    def _point(raw: Any) -> GeoPoint | None:
        try:
            lng, lat = str(raw or "").split(",", 1)
            return GeoPoint(lng=float(lng), lat=float(lat))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _place(cls, item: Any) -> VerifiedPlace | None:
        if not isinstance(item, dict):
            return None
        point = cls._point(item.get("location"))
        place_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not point or not place_id or not name:
            return None
        return VerifiedPlace(
            provider_place_id=place_id,
            name=name,
            address=cls._text(item.get("address")),
            province=cls._text(item.get("pname")),
            city=cls._text(item.get("cityname")),
            district=cls._text(item.get("adname")),
            adcode=cls._text(item.get("adcode")),
            citycode=cls._text(item.get("citycode")),
            location=point,
            typecode=cls._text(item.get("typecode")),
            verified_at=datetime.now().astimezone(),
        )

    @classmethod
    def _hotel(cls, item: Any, retrieved_at: datetime) -> HotelCandidate | None:
        if not isinstance(item, dict):
            return None
        point = cls._point(item.get("location"))
        place_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not point or not place_id or not name:
            return None
        biz = item.get("biz_ext") if isinstance(item.get("biz_ext"), dict) else {}
        rating = cls._number(biz.get("rating"), lower=0, upper=5)
        cost = cls._number(biz.get("cost"), lower=0)
        reference = ReferenceCost(amount=cost) if cost and cost > 0 else None
        photos = item.get("photos") if isinstance(item.get("photos"), list) else []
        photo_url = ""
        if photos and isinstance(photos[0], dict):
            photo_url = cls._text(photos[0].get("url"))[:1000]
        return HotelCandidate(
            provider_place_id=place_id,
            name=name,
            address=cls._text(item.get("address")),
            district=cls._text(item.get("adname")),
            business_area=cls._text(item.get("business_area")),
            location=point,
            distance_m=max(0, int(cls._number(item.get("distance"), lower=0) or 0)),
            rating=rating,
            reference_cost=reference,
            price_status="reference_only" if reference else "unknown",
            telephone=cls._text(item.get("tel")),
            photo_url=photo_url,
            retrieved_at=retrieved_at,
        )

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, list):
            return ", ".join(str(item) for item in value if item)
        return "" if value in (None, []) else str(value).strip()

    @staticmethod
    def _number(value: Any, *, lower: float, upper: float | None = None) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number < lower or (upper is not None and number > upper):
            return None
        return number
