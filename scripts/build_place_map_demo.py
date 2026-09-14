"""Build public POI/map snapshots for an isolated, credential-free UI demo."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
import httpx
from settings import AMAP_CONFIG
from core.integrations.places.service import PlaceInformationService

ROOT = Path("webui_new/static/design-demos/place-map-assets")

async def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    places = PlaceInformationService()
    data = {"generated_at": datetime.now(timezone.utc).isoformat(), "cities": []}
    async with httpx.AsyncClient(timeout=25) as client:
        for city_index, (city, queries) in enumerate([
            ("重庆", ["重庆国际会议展览中心", "重庆悦来国际会议中心", "重庆解放碑威斯汀酒店"]),
            ("南京", ["南京国际博览会议中心", "南京国际会议大酒店", "南京金陵饭店"]),
        ]):
            city_data = {"name": city, "places": []}
            for place_index, query in enumerate(queries):
                candidates = await places.search(query, city=city, limit=5)
                if not candidates:
                    raise RuntimeError(f"No city-scoped result for {query}")
                anchor = next((p for p in candidates if p.name == query), candidates[0])
                hotels = await places.nearby_hotels(anchor, limit=3, preferred_brands=["汉庭"])
                item = anchor.model_dump(mode="json")
                item["hotels"] = [h.model_dump(mode="json") for h in hotels]
                item["maps"] = {}
                for zoom in [13, 14, 15]:
                    filename = f"{city_index}-{place_index}-{zoom}.png"
                    target = ROOT / filename
                    if not target.exists():
                        response = await client.get(AMAP_CONFIG["base_url"] + "/v3/staticmap", params={
                            "key": AMAP_CONFIG["api_key"], "location": f"{anchor.location.lng},{anchor.location.lat}",
                            "zoom": zoom, "size": "750*600", "scale": 1})
                        if not response.headers.get("content-type", "").startswith("image/"):
                            body = response.json()
                            raise RuntimeError(f"Static map unavailable: {body.get('info')} {body.get('infocode')}")
                        target.write_bytes(response.content)
                    item["maps"][str(zoom)] = f"place-map-assets/{filename}"
                city_data["places"].append(item)
                print("snapshot", city, anchor.name, "hotels", len(hotels), flush=True)
            data["cities"].append(city_data)
    (ROOT / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("demo_data_ready", len(data["cities"]))

if __name__ == "__main__":
    asyncio.run(main())
