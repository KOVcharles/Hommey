"""Provider-neutral place operations shared by chat and quick-trip entry."""
from __future__ import annotations

from typing import Any

from .amap import AMapProvider
from .models import GeoPoint, HotelCandidate, VerifiedPlace


def place_matches_city(place: VerifiedPlace, city: str) -> bool:
    """Fail closed: a city hint is a geographic constraint, never a ranking hint."""
    clean = str(city or "").strip()
    if not clean:
        return False
    if clean.isdigit():
        return (len(clean) == 6 and bool(place.adcode) and
                (place.adcode == clean or clean.endswith("00") and place.adcode[:4] == clean[:4]
                 or clean in {"110000", "120000", "310000", "500000"} and place.adcode[:2] == clean[:2]))
    normalize = lambda value: str(value or "").strip().removesuffix("市")
    return normalize(clean) == normalize(place.city) or (
        normalize(clean) in {"北京", "天津", "上海", "重庆"}
        and normalize(clean) == normalize(place.province))


def validated_trip_anchor(trip: dict) -> VerifiedPlace | None:
    try:
        anchor = VerifiedPlace.model_validate(trip.get("work_location_verified"))
        if place_matches_city(anchor, trip.get("destination", "")) and anchor.name == trip.get("work_location"):
            return anchor
    except (ValueError, TypeError):
        pass
    return None


class PlaceInformationService:
    def __init__(self, provider=None):
        self.provider = provider or AMapProvider()

    @property
    def configured(self) -> bool:
        return bool(getattr(self.provider, "configured", False))

    async def search(self, keyword: str, *, city: str = "", limit: int = 5) -> list[VerifiedPlace]:
        places = await self.provider.search_places(keyword, city=city, limit=limit)
        return [p for p in places if not city or place_matches_city(p, city)]

    async def verify(self, place_id: str) -> VerifiedPlace | None:
        return await self.provider.verify_place_id(place_id)

    async def city_point(self, city: str) -> GeoPoint | None:
        """Optional origin centroid for the trip map; absent providers simply yield None."""
        geocode = getattr(self.provider, "geocode_point", None)
        return await geocode(city) if geocode else None

    async def static_map(self, place: VerifiedPlace, zoom: int) -> bytes:
        return await self.provider.static_map(place, zoom)

    async def nearby_hotels(
        self, anchor: VerifiedPlace, *, limit: int = 3, preferred_brands: list[str] | None = None,
    ) -> list[HotelCandidate]:
        kwargs = {"limit": min(limit, 20)}
        if preferred_brands:
            kwargs["preferred_brands"] = preferred_brands
        return await self.provider.nearby_hotels(anchor, **kwargs)

    async def resolve_anchor(
        self, keyword: str, *, city: str = "",
    ) -> tuple[VerifiedPlace | None, list[VerifiedPlace]]:
        candidates = await self.search(keyword, city=city, limit=5)
        if not candidates:
            return None, []
        normalized = self._normalize_name(keyword)
        exact = [
            candidate for candidate in candidates
            if self._normalize_name(candidate.name) == normalized
        ]
        if len(exact) == 1:
            return exact[0], candidates
        if len(candidates) == 1:
            return candidates[0], candidates
        return None, candidates

    @staticmethod
    def _normalize_name(value: Any) -> str:
        text = "".join(str(value or "").lower().split())
        for suffix in ("有限公司", "有限责任公司"):
            text = text.removesuffix(suffix)
        return text
