"""Shared external travel-information service used by query-info."""
from __future__ import annotations

from core.integrations.places.amap import AMapProvider
from core.integrations.places.models import (
    TransitRoutePlan,
    VerifiedPlace,
    WeatherReport,
)
from core.integrations.places.service import PlaceInformationService


class TravelInformationService:
    """Provider-neutral facade for weather, POI resolution, and local routes."""

    def __init__(self, provider=None):
        self.provider = provider or AMapProvider()
        self.places = PlaceInformationService(provider=self.provider)

    @property
    def configured(self) -> bool:
        return bool(getattr(self.provider, "configured", False))

    async def weather(self, city: str, *, adcode: str = "") -> WeatherReport | None:
        return await self.provider.weather(city, adcode=adcode)

    async def resolve_anchor(
        self, keyword: str, *, city: str = "",
    ) -> tuple[VerifiedPlace | None, list[VerifiedPlace]]:
        return await self.places.resolve_anchor(keyword, city=city)

    async def transit_routes(
        self,
        origin: VerifiedPlace,
        destination: VerifiedPlace,
        *,
        limit: int = 3,
    ) -> TransitRoutePlan:
        return await self.provider.transit_routes(origin, destination, limit=min(limit, 3))
