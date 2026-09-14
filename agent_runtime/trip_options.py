"""Bounded trip choice collection. Facts survive even if another provider fails."""
import asyncio
from datetime import date, datetime, timedelta, timezone
import re

from core.integrations.places.models import VerifiedPlace
from core.integrations.places.service import place_matches_city
from core.presentation.trip_options import TripOptions, ChoiceState, TrainChoice, HotelChoice
from core.presentation.answer_document import AnswerDocument, AnswerSection, render_plain_text
from core.trip_intake import beijing_today
from utils.io_executor import run_blocking


def exclusions(text, selection):
    excluded = set((selection or {}).get("exclude", []))
    for key, words in {"train": "车次|高铁|火车", "nearby_hotels": "酒店|住宿", "weather": "天气", "local_transport": "市内交通|通勤"}.items():
        if re.search(rf"(?:不要|不用|不需要|不查)[^，。；\n]{{0,8}}(?:{words})", text):
            excluded.add(key)
    return excluded


def rank_trains(rows, seat=""):
    def duration(row):
        parts = str(row.get("duration", "")).split(":")
        return int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 and all(p.isdigit() for p in parts) else 99999
    def available(row):
        seats = row.get("seats", {})
        values = [seats[seat]] if seat in seats else list(seats.values())
        return any(str(v) not in {"", "无", "--", "0", "候补"} for v in values)
    unique = {(r.get("train_no"), r.get("from_station"), r.get("to_station")): r for r in rows}
    return sorted(unique.values(), key=lambda r: (not available(r), duration(r), r.get("depart_time", "")))[:12]


async def collect_options(services, trip, *, selection=None, user_text=""):
    from core.integrations.travel_info import TravelInformationService
    from core.integrations.trains import create_train_query_backend
    if services.travel is None:
        services.travel = TravelInformationService()
    if services.trains is None:
        services.trains = create_train_query_backend()
    prefs = await run_blocking(services.memory.long_term.get_preference)
    prefs = prefs if isinstance(prefs, dict) else {}
    selection = selection or {}
    excluded = exclusions(user_text, selection)
    start = date.fromisoformat(trip["start_date"])
    end = date.fromisoformat(trip["end_date"]) if trip.get("end_date") else start + timedelta(days=int(trip["duration_days"]) - 1)
    board = TripOptions(origin=trip["origin"], destination=trip["destination"], start_date=start.isoformat(),
        end_date=end.isoformat(), duration_days=(end-start).days+1, trip_purpose=trip["trip_purpose"],
        work_schedule=trip.get("work_schedule") or "", location_query=trip.get("work_location") or "",
        train_state=ChoiceState(status="needs_input"), hotel_state=ChoiceState(status="needs_input"),
        capability_selection={"include": list(selection.get("include", [])), "exclude": sorted(excluded)})
    raw_anchor = trip.get("work_location_verified")
    if raw_anchor:
        try:
            anchor = VerifiedPlace.model_validate(raw_anchor)
            if place_matches_city(anchor, board.destination) and anchor.name == trip.get("work_location"):
                board.anchor = anchor
        except ValueError:
            pass
    brands = prefs.get("hotel_brands") or []
    brands = brands if isinstance(brands, list) else [brands]
    seat = str(prefs.get("seat_preference") or "")
    board.preference_note = " · ".join(filter(None, ["偏好 " + "、".join(brands) if brands else "", "席位偏好 " + seat if seat else ""]))

    async def trains():
        if "train" in excluded:
            board.train_state = ChoiceState(status="excluded", message="本次未查询车次。")
            return
        if start < date.fromisoformat(beijing_today()):
            board.train_state = ChoiceState(status="needs_input", message="出发日期已过，请先修改日期再查询车次。")
            return
        try:
            query = getattr(services.trains, "query_city_trains", services.trains.query_trains)
            rows = await asyncio.wait_for(query(board.origin, board.destination, board.start_date), timeout=24)
            for row in rank_trains(rows, seat):
                offset = 0
                try:
                    hours, minutes = map(int, row["depart_time"].split(":"))
                    dh, dm = map(int, row["duration"].split(":"))
                    offset = (hours * 60 + minutes + dh * 60 + dm) // 1440
                except (KeyError, ValueError):
                    pass
                board.trains.append(TrainChoice(**{key: row.get(key, "") for key in (
                    "train_no", "from_station", "to_station", "depart_time", "arrive_time", "duration")},
                    travel_date=board.start_date, arrival_day_offset=offset, seats={str(k): str(v) for k,v in row.get("seats", {}).items()},
                    reason="优先显示有余票、历时较短的车次；请结合会议开始时间选择"))
            board.train_state = ChoiceState(status="ready" if board.trains else "empty",
                message="12306 余票查询 · 票价请在 12306 核实" if board.trains else "本次查询未返回直达车次，可修改日期或具体车站后重查。",
                retrieved_at=datetime.now(timezone.utc).isoformat())
        except Exception:
            board.train_state = ChoiceState(status="unavailable", message="12306 本次查询未成功，可以重试或前往 12306 核实。")

    async def hotels():
        if "nearby_hotels" in excluded:
            board.hotel_state = ChoiceState(status="excluded", message="本次未查询酒店。")
            return
        if not board.anchor:
            board.hotel_state = ChoiceState(status="needs_input", message=f"请在{board.destination}范围内选择会议或办公地点，随后查找附近酒店。")
            return
        try:
            candidates = await asyncio.wait_for(services.travel.places.nearby_hotels(board.anchor, limit=20, preferred_brands=brands), timeout=18)
            match = lambda hotel: any(str(brand).strip() and str(brand).strip() in hotel.name for brand in brands)
            candidates = sorted(candidates, key=lambda h: (not match(h), h.distance_m, -(h.rating or 0), h.provider_place_id))
            for hotel in candidates[:4]:
                reason = "符合酒店品牌偏好" if match(hotel) else "按距工作地点由近到远排列"
                board.hotels.append(HotelChoice(hotel=hotel, reason=reason, matches_brand=match(hotel)))
            board.hotel_state = ChoiceState(status="ready" if board.hotels else "empty",
                message="高德附近酒店 · 参考消费不是入住日期房价，房态及差标需另行确认" if board.hotels else "该地点周边未返回酒店，可重新选择地点。",
                retrieved_at=datetime.now(timezone.utc).isoformat())
        except Exception:
            board.hotel_state = ChoiceState(status="unavailable", message="高德酒店查询暂时不可用；已保留所选地点，可重试。")
    async def origin_point():
        # Only the trip map draws a route, and a route needs both ends. Skip the lookup when
        # there is no anchor yet; on failure the map degrades to just the anchor, silently.
        if not board.anchor:
            return
        try:
            board.origin_point = await asyncio.wait_for(services.travel.places.city_point(board.origin), timeout=8)
        except Exception:
            pass
    await asyncio.gather(trains(), hotels(), origin_point())
    included = set(selection.get("include", [])) - excluded
    async def weather():
        if "weather" not in included:
            return
        try:
            board.weather = await asyncio.wait_for(services.travel.weather(board.destination), timeout=12)
            if not board.weather:
                board.next_actions.append("本次未查到目的地天气。")
        except Exception:
            board.next_actions.append("天气暂未查询成功，车次和酒店结果已保留。")
    async def commute():
        if "local_transport" not in included:
            return
        if not board.anchor or not board.trains:
            board.next_actions.append("确认工作地点且有到达车站后，可继续查询到会场的公共交通。")
            return
        station = board.trains[0].to_station
        try:
            async def query():
                origin, _ = await services.travel.resolve_anchor(station + ("" if station.endswith("站") else "站"), city=board.destination)
                if origin:
                    return await services.travel.transit_routes(origin, board.anchor)
            board.commute = await asyncio.wait_for(query(), timeout=12)
            if not board.commute or not board.commute.options:
                board.next_actions.append(f"暂未查到{station}至工作地点的公共交通路线。")
        except Exception:
            board.next_actions.append("到站通勤查询未成功，可通过酒店地图入口核实位置。")
    await asyncio.gather(weather(), commute())
    if not board.work_schedule:
        board.next_actions.append("补充会议开始时间后，再确认所选车次能否留足到会场的时间。")
    return board


def options_output(board):
    parts = [f"去程 {board.start_date}：{board.train_state.message}"]
    parts.extend(f"{r.train_no} {r.from_station} {r.depart_time} → {r.to_station} {r.arrive_time}，历时 {r.duration}" for r in board.trains)
    parts.append(board.hotel_state.message)
    parts.extend(f"{r.hotel.name}，距工作地点约 {r.hotel.distance_m} 米，{r.reason}" for r in board.hotels)
    if board.commute:
        parts.append(f"通勤参考：{board.commute.origin.name} → {board.commute.destination.name}")
    document = AnswerDocument(title=f"{board.origin} → {board.destination}", summary=f"{board.start_date} — {board.end_date} · {board.duration_days} 天 · {board.trip_purpose}",
        trip_options=board, sections=[AnswerSection(kind="trip", title="出行选择", body="\n".join(parts)[:4000])], notices=board.next_actions)
    document.plain_text = render_plain_text(document)
    statuses = {board.train_state.status, board.hotel_state.status}
    return {"response": document.plain_text, "answer_document": document.model_dump(mode="json"), "presentation_document": None,
        "outcome": "waiting_input" if "needs_input" in statuses else "partial" if statuses & {"unavailable", "empty"} else "completed"}
