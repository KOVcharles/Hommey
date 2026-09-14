"""Read-only provider smoke check; never writes employee memory or books travel."""
import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from agent_runtime.trip_options import collect_options, options_output
from core.integrations.travel_info import TravelInformationService
from core.integrations.trains import create_train_query_backend


async def main():
    travel = TravelInformationService()
    places = await travel.places.search("国际会议展览中心", city="重庆")
    assert places and all(p.city == "重庆市" for p in places)
    anchor = await travel.places.verify(places[0].provider_place_id)
    service = SimpleNamespace(travel=travel, trains=create_train_query_backend(),
        memory=SimpleNamespace(long_term=SimpleNamespace(get_preference=lambda: {"hotel_brands": ["汉庭"]})))
    trip = {"origin": "北京", "destination": "重庆", "start_date": (date.today()+timedelta(days=1)).isoformat(),
        "duration_days": 2, "trip_purpose": "界面联调示例", "work_location": anchor.name,
        "work_location_verified": anchor.model_dump(mode="json")}
    board = await collect_options(service, trip)
    output = options_output(board)
    target = Path("webui_new/static/design-demos/trip-choices-live.json")
    target.write_text(json.dumps(output["answer_document"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"city": anchor.city, "anchor": anchor.name, "train": board.train_state.model_dump(),
        "hotel": board.hotel_state.model_dump(), "train_count": len(board.trains),
        "hotels": [{"name": h.hotel.name, "distance": h.hotel.distance_m, "reason": h.reason} for h in board.hotels]}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
