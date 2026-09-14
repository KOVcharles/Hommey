import asyncio
import json

import httpx

from core.integrations.places.amap import AMapProvider
from core.integrations.places.models import GeoPoint, VerifiedPlace


def _place(place_id="P1", name="阿里巴巴西溪园区"):
    from datetime import datetime

    return VerifiedPlace(
        provider_place_id=place_id,
        name=name,
        address="杭州市余杭区文一西路969号",
        city="杭州市",
        district="余杭区",
        adcode="330110",
        citycode="0571",
        location=GeoPoint(lng=120.027, lat=30.279),
        verified_at=datetime.now().astimezone(),
    )


def test_amap_provider_normalizes_and_limits_nearby_hotels():
    async def handler(request: httpx.Request):
        assert request.url.path == "/v3/place/around"
        return httpx.Response(200, json={
            "status": "1",
            "pois": [
                {
                    "id": f"H{index}", "name": f"酒店{index}",
                    "address": f"示例路{index}号", "adname": "余杭区",
                    "location": f"120.0{index},30.2{index}",
                    "distance": str(distance),
                    "biz_ext": {"rating": str(rating), "cost": cost},
                }
                for index, distance, rating, cost in (
                    (1, 900, 4.5, "520"),
                    (2, 300, 4.2, []),
                    (3, 600, 4.8, "680"),
                    (4, 1200, 4.9, "800"),
                )
            ],
        })

    client = httpx.AsyncClient(
        base_url="https://restapi.amap.com",
        transport=httpx.MockTransport(handler),
    )
    provider = AMapProvider(
        config={
            "enabled": True,
            "api_key": "test-key",
            "base_url": "https://restapi.amap.com",
            "timeout_sec": 1,
            "hotel_radius_m": 5000,
        },
        client=client,
    )
    try:
        hotels = asyncio.run(provider.nearby_hotels(_place(), limit=3))
    finally:
        asyncio.run(client.aclose())

    assert [hotel.name for hotel in hotels] == ["酒店2", "酒店3", "酒店1"]
    assert len(hotels) == 3
    assert hotels[0].reference_cost is None
    assert hotels[0].price_status == "unknown"
    assert hotels[1].reference_cost.amount == 680
    assert hotels[1].reference_cost.realtime is False


def test_amap_provider_resolves_adcode_and_normalizes_weather():
    async def handler(request: httpx.Request):
        if request.url.path == "/v3/geocode/geo":
            assert request.url.params["address"] == "杭州"
            return httpx.Response(200, json={
                "status": "1",
                "geocodes": [{"adcode": "330100", "location": "120.1551,30.2741"}],
            })
        assert request.url.path == "/v3/weather/weatherInfo"
        if request.url.params["extensions"] == "base":
            return httpx.Response(200, json={
                "status": "1",
                "lives": [{
                    "province": "浙江", "city": "杭州市", "adcode": "330100",
                    "weather": "多云", "temperature": "29", "humidity": "68",
                    "winddirection": "东", "windpower": "≤3",
                    "reporttime": "2026-08-30 10:00:00",
                }],
            })
        return httpx.Response(200, json={
            "status": "1",
            "forecasts": [{
                "province": "浙江", "city": "杭州市", "adcode": "330100",
                "casts": [{
                    "date": "2026-08-30", "dayweather": "多云", "nightweather": "小雨",
                    "daytemp": "31", "nighttemp": "24", "daywind": "东",
                    "nightwind": "东", "daypower": "≤3", "nightpower": "≤3",
                }],
            }],
        })

    client = httpx.AsyncClient(
        base_url="https://restapi.amap.com", transport=httpx.MockTransport(handler),
    )
    provider = AMapProvider(config={
        "enabled": True, "api_key": "test-key", "base_url": "https://restapi.amap.com",
        "timeout_sec": 1,
    }, client=client)
    try:
        report = asyncio.run(provider.weather("杭州"))
    finally:
        asyncio.run(client.aclose())

    assert report.provider == "amap"
    assert report.city == "杭州市"
    assert report.adcode == "330100"
    assert report.current.temperature_c == 29
    assert report.current.humidity_pct == 68
    assert report.forecasts[0].low_c == 24
    assert report.forecasts[0].high_c == 31


def test_amap_provider_normalizes_transit_routes_between_verified_pois():
    async def handler(request: httpx.Request):
        assert request.url.path == "/v5/direction/transit/integrated"
        assert request.url.params["city1"] == "0571"
        assert request.url.params["city2"] == "0571"
        return httpx.Response(200, json={
            "status": "1",
            "route": {"transits": [{
                "distance": "12800",
                "cost": {"duration": "2520", "transit_fee": "6"},
                "segments": [{
                    "bus": {"buslines": [{"name": "地铁5号线"}, {"name": "地铁19号线"}]},
                }],
            }]},
        })

    destination = _place("P2", "杭州东站").model_copy(update={
        "location": GeoPoint(lng=120.212, lat=30.291),
    })
    client = httpx.AsyncClient(
        base_url="https://restapi.amap.com", transport=httpx.MockTransport(handler),
    )
    provider = AMapProvider(config={
        "enabled": True, "api_key": "test-key", "base_url": "https://restapi.amap.com",
        "timeout_sec": 1,
    }, client=client)
    try:
        plan = asyncio.run(provider.transit_routes(_place(), destination))
    finally:
        asyncio.run(client.aclose())

    assert len(plan.options) == 1
    assert plan.options[0].duration_sec == 2520
    assert plan.options[0].transit_fee_cny == 6
    assert plan.options[0].lines == ["地铁5号线", "地铁19号线"]
