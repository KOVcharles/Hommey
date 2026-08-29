"""Authenticated place suggestions for the quick-trip form."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from core.integrations.places.amap import AMapError
from webui_new.auth import User, require_path_user
from webui_new.core.errors import BusinessError


def create_places_router(place_service):
    router = APIRouter()

    @router.get("/api/{user_id}/places/suggest")
    async def suggest_places(
        user_id: str,
        keyword: str = Query(min_length=2, max_length=80),
        city: str = Query(default="", max_length=80),
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
                }
                for place in places
            ]
        }

    return router
