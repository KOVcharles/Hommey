"""Provider-neutral place operations shared by chat and quick-trip entry."""
from __future__ import annotations

from typing import Any

from .amap import AMapProvider
from .models import HotelCandidate, VerifiedPlace


class PlaceInformationService:
    def __init__(self, provider=None):
        self.provider = provider or AMapProvider()

    @property
    def configured(self) -> bool:
        return bool(getattr(self.provider, "configured", False))

    async def search(self, keyword: str, *, city: str = "", limit: int = 5) -> list[VerifiedPlace]:
        return await self.provider.search_places(keyword, city=city, limit=limit)

    async def verify(self, place_id: str) -> VerifiedPlace | None:
        return await self.provider.verify_place_id(place_id)

    async def nearby_hotels(
        self, anchor: VerifiedPlace, *, limit: int = 3,
    ) -> list[HotelCandidate]:
        return await self.provider.nearby_hotels(anchor, limit=min(limit, 3))

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
