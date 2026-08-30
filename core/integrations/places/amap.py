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

from .models import (
    GeoPoint,
    HotelCandidate,
    ReferenceCost,
    TransitRouteOption,
    TransitRoutePlan,
    VerifiedPlace,
    WeatherCurrent,
    WeatherForecastDay,
    WeatherReport,
)

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

    async def weather(self, city: str, *, adcode: str = "") -> WeatherReport | None:
        """Return normalized mainland live weather and short-range forecast."""
        clean_city = str(city or "").strip()[:80]
        clean_adcode = str(adcode or "").strip()[:20]
        if not clean_adcode:
            clean_adcode = await self.resolve_adcode(clean_city)
        if not clean_adcode:
            return None

        live_payload, forecast_payload = await asyncio.gather(
            self._request("/v3/weather/weatherInfo", {
                "city": clean_adcode, "extensions": "base",
            }),
            self._request("/v3/weather/weatherInfo", {
                "city": clean_adcode, "extensions": "all",
            }),
        )
        live = next(
            (item for item in live_payload.get("lives") or [] if isinstance(item, dict)),
            {},
        )
        forecast = next(
            (
                item for item in forecast_payload.get("forecasts") or []
                if isinstance(item, dict)
            ),
            {},
        )
        resolved_city = self._text(live.get("city") or forecast.get("city") or clean_city)
        if not resolved_city:
            return None
        current = None
        if live:
            current = WeatherCurrent(
                condition=self._text(live.get("weather")),
                temperature_c=self._number(live.get("temperature"), lower=-100, upper=100),
                humidity_pct=self._number(live.get("humidity"), lower=0, upper=100),
                wind_direction=self._text(live.get("winddirection")),
                wind_power=self._text(live.get("windpower")),
                report_time=self._text(live.get("reporttime")),
            )
        forecasts = []
        for item in forecast.get("casts") or []:
            if not isinstance(item, dict) or not self._text(item.get("date")):
                continue
            day_temp = self._number(item.get("daytemp"), lower=-100, upper=100)
            night_temp = self._number(item.get("nighttemp"), lower=-100, upper=100)
            forecasts.append(WeatherForecastDay(
                date=self._text(item.get("date")),
                day_condition=self._text(item.get("dayweather")),
                night_condition=self._text(item.get("nightweather")),
                low_c=min(day_temp, night_temp) if day_temp is not None and night_temp is not None else night_temp,
                high_c=max(day_temp, night_temp) if day_temp is not None and night_temp is not None else day_temp,
                day_wind=self._text(item.get("daywind")),
                night_wind=self._text(item.get("nightwind")),
                day_power=self._text(item.get("daypower")),
                night_power=self._text(item.get("nightpower")),
            ))
        return WeatherReport(
            province=self._text(live.get("province") or forecast.get("province")),
            city=resolved_city,
            adcode=self._text(live.get("adcode") or forecast.get("adcode") or clean_adcode),
            current=current,
            forecasts=forecasts[:4],
            retrieved_at=datetime.now().astimezone(),
        )

    async def resolve_adcode(self, city: str) -> str:
        clean_city = str(city or "").strip()[:80]
        if not clean_city:
            return ""
        if clean_city.isdigit() and 6 <= len(clean_city) <= 12:
            return clean_city
        payload = await self._request("/v3/geocode/geo", {"address": clean_city})
        for item in payload.get("geocodes") or []:
            if not isinstance(item, dict):
                continue
            adcode = self._text(item.get("adcode"))
            point = self._point(item.get("location"))
            if adcode and point:
                return adcode
        return ""

    async def transit_routes(
        self,
        origin: VerifiedPlace,
        destination: VerifiedPlace,
        *,
        limit: int = 3,
    ) -> TransitRoutePlan:
        """Plan public-transit routes only between server-verified POIs."""
        if not origin.citycode or not destination.citycode:
            return TransitRoutePlan(
                origin=origin, destination=destination, options=[],
                retrieved_at=datetime.now().astimezone(),
            )
        payload = await self._request("/v5/direction/transit/integrated", {
            "origin": self._format_point(origin.location),
            "destination": self._format_point(destination.location),
            "originpoi": origin.provider_place_id,
            "destinationpoi": destination.provider_place_id,
            "ad1": origin.adcode,
            "ad2": destination.adcode,
            "city1": origin.citycode,
            "city2": destination.citycode,
            "strategy": 0,
            "AlternativeRoute": max(1, min(int(limit), 3)),
            "show_fields": "cost",
        })
        route = payload.get("route") if isinstance(payload.get("route"), dict) else {}
        options = []
        for item in route.get("transits") or []:
            if not isinstance(item, dict):
                continue
            cost = item.get("cost") if isinstance(item.get("cost"), dict) else {}
            duration = self._number(
                cost.get("duration") if cost else item.get("duration"), lower=0,
            )
            fee = self._number(
                cost.get("transit_fee") if cost else item.get("transit_fee"), lower=0,
            )
            options.append(TransitRouteOption(
                distance_m=max(0, int(self._number(item.get("distance"), lower=0) or 0)),
                duration_sec=int(duration) if duration is not None else None,
                transit_fee_cny=fee,
                lines=self._transit_lines(item)[:12],
            ))
        return TransitRoutePlan(
            origin=origin,
            destination=destination,
            options=options[:max(1, min(int(limit), 3))],
            retrieved_at=datetime.now().astimezone(),
        )

    async def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise AMapError("高德 Web 服务尚未配置")
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
            raise AMapError("高德 Web 服务暂时不可用") from last_error
        if not isinstance(payload, dict) or str(payload.get("status")) != "1":
            info_code = str(payload.get("infocode") or "UNKNOWN") if isinstance(payload, dict) else "MALFORMED"
            logger.warning("amap_provider_error path=%s infocode=%s", path, info_code[:32])
            raise AMapError("高德 Web 服务返回失败")
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

    @staticmethod
    def _format_point(point: GeoPoint) -> str:
        return f"{point.lng:.6f},{point.lat:.6f}"

    @classmethod
    def _transit_lines(cls, transit: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        for segment in transit.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            bus = segment.get("bus") if isinstance(segment.get("bus"), dict) else {}
            buslines = bus.get("buslines") or bus.get("busline") or []
            if isinstance(buslines, dict):
                buslines = [buslines]
            for line in buslines:
                if not isinstance(line, dict):
                    continue
                name = cls._text(line.get("name"))
                if name and name not in lines:
                    lines.append(name)
            railway = segment.get("railway") if isinstance(segment.get("railway"), dict) else {}
            railway_name = cls._text(railway.get("name") or railway.get("trip"))
            if railway_name and railway_name not in lines:
                lines.append(railway_name)
        return lines

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
