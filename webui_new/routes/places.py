"""Authenticated place suggestions for the quick-trip form."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from core.integrations.places.amap import MAX_MAP_ZOOM, MIN_MAP_ZOOM, AMapError
from core.integrations.places.service import place_matches_city
from webui_new.auth import User, require_path_user
from webui_new.core.errors import BusinessError


def create_places_router(place_service):
    router = APIRouter()

    @router.get("/api/{user_id}/places/suggest")
    async def suggest_places(
        user_id: str,
        keyword: str = Query(min_length=2, max_length=80),
        city: str = Query(min_length=2, max_length=80),
        current_user: User = Depends(require_path_user),
    ):
        if not place_service.configured:
            raise BusinessError("PLACE_SERVICE_NOT_CONFIGURED", "地点服务尚未配置，请联系管理员")
        try:
            places = await place_service.search(keyword, city=city, limit=5)
        except AMapError as exc:
            raise BusinessError("PLACE_SEARCH_FAILED", "地点查询暂时不可用，请稍后重试") from exc
        return {
            "items": [
                {
                    "place_id": place.provider_place_id,
                    "name": place.name,
                    "address": place.address,
                    "city": place.city,
                    "district": place.district,
                    "provider": place.provider,
                    "location": place.location.model_dump(),
                }
                for place in places if place_matches_city(place, city)
            ]
        }

    @router.get("/api/{user_id}/places/map")
    async def place_map(
        user_id: str,
        place_id: str = Query(min_length=1, max_length=80),
        city: str = Query(min_length=2, max_length=80),
        zoom: int = Query(default=13, ge=MIN_MAP_ZOOM, le=MAX_MAP_ZOOM),
        current_user: User = Depends(require_path_user),
    ):
        try:
            place = await place_service.verify(place_id)
            if place is None or not place_matches_city(place, city):
                raise BusinessError("PLACE_CITY_MISMATCH", "所选地点不属于目的地，请重新选择")
            image = await place_service.static_map(place, zoom)
        except AMapError as exc:
            raise BusinessError("PLACE_MAP_UNAVAILABLE", "地图暂时不可用，仍可查看地点与酒店信息") from exc
        return Response(image, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})

    return router
