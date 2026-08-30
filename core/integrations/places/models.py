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


class WeatherCurrent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: str = Field(default="", max_length=80)
    temperature_c: float | None = None
    humidity_pct: float | None = Field(default=None, ge=0, le=100)
    wind_direction: str = Field(default="", max_length=40)
    wind_power: str = Field(default="", max_length=40)
    report_time: str = Field(default="", max_length=80)


class WeatherForecastDay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str = Field(min_length=1, max_length=40)
    day_condition: str = Field(default="", max_length=80)
    night_condition: str = Field(default="", max_length=80)
    low_c: float | None = None
    high_c: float | None = None
    day_wind: str = Field(default="", max_length=40)
    night_wind: str = Field(default="", max_length=40)
    day_power: str = Field(default="", max_length=40)
    night_power: str = Field(default="", max_length=40)


class WeatherReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["amap"] = "amap"
    province: str = Field(default="", max_length=80)
    city: str = Field(min_length=1, max_length=80)
    adcode: str = Field(min_length=1, max_length=20)
    current: WeatherCurrent | None = None
    forecasts: list[WeatherForecastDay] = Field(default_factory=list, max_length=4)
    retrieved_at: datetime


class TransitRouteOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["transit"] = "transit"
    distance_m: int = Field(ge=0)
    duration_sec: int | None = Field(default=None, ge=0)
    transit_fee_cny: float | None = Field(default=None, ge=0)
    lines: list[str] = Field(default_factory=list, max_length=12)


class TransitRoutePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["amap"] = "amap"
    origin: VerifiedPlace
    destination: VerifiedPlace
    options: list[TransitRouteOption] = Field(default_factory=list, max_length=3)
    retrieved_at: datetime
