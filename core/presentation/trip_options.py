"""Provider-backed choices, kept separate from model-authored prose."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from core.integrations.places.models import GeoPoint, VerifiedPlace, HotelCandidate, WeatherReport, TransitRoutePlan


class ChoiceState(BaseModel):
    status: Literal["ready", "empty", "unavailable", "needs_input", "excluded"]
    message: str = ""
    retrieved_at: str = ""


class TrainChoice(BaseModel):
    train_no: str
    from_station: str
    to_station: str
    depart_time: str
    arrive_time: str
    duration: str = ""
    travel_date: str
    seats: dict[str, str] = Field(default_factory=dict)
    reason: str = ""
    arrival_day_offset: int = 0


class HotelChoice(BaseModel):
    hotel: HotelCandidate
    reason: str
    matches_brand: bool = False


class TripOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str
    destination: str
    start_date: str
    end_date: str
    duration_days: int
    trip_purpose: str
    work_schedule: str = ""
    location_query: str = ""
    anchor: VerifiedPlace | None = None
    # Origin centroid, so the map can draw the trip from where the traveller starts.
    # Stays null when geocoding is unavailable; the map then shows the anchor alone.
    origin_point: GeoPoint | None = None
    train_state: ChoiceState
    hotel_state: ChoiceState
    trains: list[TrainChoice] = Field(default_factory=list, max_length=12)
    hotels: list[HotelChoice] = Field(default_factory=list, max_length=4)
    preference_note: str = ""
    next_actions: list[str] = Field(default_factory=list)
    capability_selection: dict[str, list[str]] = Field(default_factory=dict)
    weather: WeatherReport | None = None
    commute: TransitRoutePlan | None = None
