"""Strict adapter for the existing intake card's labelled wire text.

Only accept a complete sequence of known field encodings. Mixed requests,
relative dates and ambiguous text remain on the normal extraction path.
"""
import re

from .contracts import TripData


PATTERNS = (
    ("origin", r"从([^，,\n]{1,80})出发"),
    ("destination", r"目的地[：:]([^，,\n]{1,80})"),
    ("start_date", r"(\d{4}-\d{2}-\d{2})出发"),
    ("duration_days", r"出差([1-9]\d?)天"),
    ("end_date", r"(\d{4}-\d{2}-\d{2})返程"),
    ("trip_purpose", r"出差目的[：:]([^，,\n]{1,300})"),
    ("work_location", r"工作地点[：:]([^，,\n]{1,300})"),
    ("work_schedule", r"工作时间[：:]([^，,\n]{1,300})"),
)


def parse_trip_entry(text):
    """A whole, unqualified trip declaration; extra intents stay with the model.

    This only extracts literal places, never dates, transport or purpose. The
    caller must use the normal grounded-write gate and evaluate persisted state.
    """
    match = re.fullmatch(
        r"\s*(?:我)?(?:要|想|准备|计划)?(?:从(?P<origin>[\u4e00-\u9fff]{2,12}))?"
        r"去(?P<destination>[\u4e00-\u9fff]{2,12})出差[。！!\s]*", text)
    if not match:
        return None
    trip = {k: v for k, v in match.groupdict().items() if v}
    if any(word in value for value in trip.values() for word in (
        "和", "与", "或", "再", "然后", "顺便", "下周", "明天", "今天", "后天", "不", "不是", "改成")):
        return None
    return TripData(trip=trip, field_sources={k: text.strip() for k in trip}).model_dump(exclude_none=True)


def parse_intake_submission(text):
    trip, quotes = {}, {}
    for part in text.strip().split("，"):
        for key, pattern in PATTERNS:
            match = re.fullmatch(pattern, part.strip())
            if match:
                if key in trip:
                    return None
                trip[key] = int(match[1]) if key == "duration_days" else match[1]
                quotes[key] = part.strip()
                break
        else:
            return None
    # A single casual phrase like '从北京出发' is not a submitted form.
    if len(trip) < 2 and not any(k in trip for k in ("destination", "trip_purpose", "work_location", "work_schedule")):
        return None
    return TripData(trip=trip, field_sources=quotes).model_dump(exclude_none=True)
