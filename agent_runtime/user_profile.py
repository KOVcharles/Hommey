"""Model-facing views of saved personal facts, funding projects and travel preferences."""
from copy import deepcopy
from datetime import date

from core.user_profile import PersonalProfile

from context.preference_schema import PREFERENCE_LIST_COLUMNS, PREFERENCE_SCALAR_COLUMNS
from context.profile_catalog import InvalidProfileValue, normalize_profile_value


def profile_from_preferences(preferences):
    """Whitelist known fields; never infer identity or copy arbitrary extra data."""
    if not isinstance(preferences, dict):
        return {}
    values, truncated = {}, []
    for key in (*PREFERENCE_SCALAR_COLUMNS, *PREFERENCE_LIST_COLUMNS):
        if preferences.get(key) is None:
            continue
        try:
            value = normalize_profile_value(key, preferences[key]).value
        except InvalidProfileValue:
            continue
        if isinstance(value, list) and len(value) > 4:
            truncated.append(key)
            value = value[:4]
        values[key] = value
    profile = {}
    if origin := values.pop("home_location", None):
        profile["defaults"] = {"origin": origin}
    if values:
        profile["preferences"] = values
    if truncated:
        profile["truncated_fields"] = truncated
    return {"source": "saved_preferences", **profile} if profile else {}


def profile_from_personal(personal, preferences, reference_date):
    """Project user-entered facts; account identifiers and full birthday stay in storage."""
    view = profile_from_preferences(preferences)
    profile = PersonalProfile.model_validate(personal)
    basic = profile.basic_info
    identity = {key: value for key, value in profile.policy_identity.model_dump().items() if value is not None}
    for key in ("institution", "department", "personnel_category", "nationality"):
        if value := getattr(basic, key):
            identity[key] = value
    if basic.birth_date:
        as_of = date.fromisoformat(str(reference_date)[:10])
        if as_of >= basic.birth_date:
            identity["age_years"] = as_of.year - basic.birth_date.year - ((as_of.month, as_of.day) < (basic.birth_date.month, basic.birth_date.day))
            identity["age_as_of"] = as_of.isoformat()
    if identity:
        view["identity"] = identity
    if profile.funding.projects:
        # Keep every project, including unknown fields and explicit False values.
        # IDs distinguish projects with the same name and identify the saved default.
        view["funding"] = profile.funding.model_dump(mode="json")
    default = next((project for project in profile.funding.projects if project.id == profile.funding.default_project_id), None) if profile.funding.default_project_id else None
    if default:
        # Compatibility summary of the same project; funding is the complete catalog.
        view["default_funding"] = {key: value for key, value in default.model_dump().items()
            if key != "id" and value is not None}
    if profile.settlement.has_official_card is not None:
        view["settlement"] = profile.settlement.model_dump()
    if any(key in view for key in ("identity", "funding", "settlement")):
        view.update(source="saved_personal_profile", verification="user_entered")
    return view


def profile_for_role(profile, role=None):
    """Give each specialist only the saved facts relevant to its work."""
    if not profile:
        return {}
    if role in {None, "memory", "trip_planner"}:
        return deepcopy(profile)
    view = {}
    if role == "trip_context" and profile.get("defaults"):
        view["defaults"] = deepcopy(profile["defaults"])
    if role == "travel_info":
        preferences = {k: v for k, v in profile.get("preferences", {}).items()
                       if k in {"hotel_brands", "airlines", "seat_preference", "transportation_preference"}}
        if preferences:
            view["preferences"] = deepcopy(preferences)
            truncated = [k for k in profile.get("truncated_fields", []) if k in preferences]
            if truncated:
                view["truncated_fields"] = truncated
    if role in {"policy_rag", "compliance"}:
        for key in ("identity", "funding", "default_funding", "settlement"):
            if profile.get(key):
                view[key] = deepcopy(profile[key])
    if not view:
        return {}
    result = {"source": profile.get("source", "saved_preferences"), **view}
    if "verification" in profile:
        result["verification"] = profile["verification"]
    return result
