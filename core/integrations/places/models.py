"""Strict normalized contracts for third-party place data."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GeoPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lng: float = Field(ge=73.0, le=136.0)
    lat: float = Field(ge=3.0, le=54.0)


class VerifiedPlace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["amap"] = "amap"
    provider_place_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(default="", max_length=500)
    province: str = Field(default="", max_length=80)
    city: str = Field(default="", max_length=80)
    district: str = Field(default="", max_length=80)
    adcode: str = Field(default="", max_length=20)
    citycode: str = Field(default="", max_length=20)
    location: GeoPoint
    typecode: str = Field(default="", max_length=32)
    verified: Literal[True] = True
    verified_at: datetime


class ReferenceCost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: float = Field(gt=0)
    currency: Literal["CNY"] = "CNY"
    source: Literal["amap"] = "amap"
    realtime: Literal[False] = False
    label: Literal["高德参考消费"] = "高德参考消费"


class HotelCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_place_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(default="", max_length=500)
    district: str = Field(default="", max_length=80)
    business_area: str = Field(default="", max_length=120)
    location: GeoPoint
    distance_m: int = Field(ge=0)
    rating: float | None = Field(default=None, ge=0, le=5)
    reference_cost: ReferenceCost | None = None
    price_status: Literal["reference_only", "unknown"] = "unknown"
    telephone: str = Field(default="", max_length=120)
    photo_url: str = Field(default="", max_length=1000)
    source: Literal["amap"] = "amap"
    retrieved_at: datetime
