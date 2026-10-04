"""First-run travel preference onboarding for the WebUI."""
from typing import Any, Dict, List

from utils.memory_safety import is_safe_preference_value


class InitialPreferenceOnboarding:
    """Small coordinator for first-run preference setup."""

    REQUIRED_KEYS = (
        "home_location",
        "transportation_preference",
        "hotel_brands",
        "seat_preference",
    )

    DISPLAY_NAMES = {
        "home_location": "常驻城市",
        "transportation_preference": "交通偏好",
        "hotel_brands": "酒店偏好",
        "seat_preference": "座位偏好",
    }

    LIST_KEYS = {"hotel_brands"}
    EMPTY_VALUES = {"", "不指定", "暂不指定", "无", "没有"}


    def get_state(self, memory_manager) -> Dict[str, Any]:
        """Return onboarding progress based on current long-term preferences."""
        prefs = self._get_preferences(memory_manager)
        missing = [key for key in self.REQUIRED_KEYS if not self._has_value(prefs.get(key))]
        completed = [key for key in self.REQUIRED_KEYS if key not in missing]
        return {
            "is_new": bool(missing),
            "completed": not missing,
            "completed_keys": completed,
            "missing_keys": missing,
            "preferences": prefs,
        }


    def needs_onboarding(self, memory_manager) -> bool:
        """Whether the user still needs the first-run preference flow."""
        return bool(self.get_state(memory_manager)["missing_keys"])

    def save_answer(self, memory_manager, key: str, value: str) -> Dict[str, Any]:
        """Persist one onboarding answer using explicit preference keys."""
        if key not in self.REQUIRED_KEYS:
            raise ValueError(f"Unsupported onboarding preference: {key}")

        clean_value = self._clean_value(value)
        if clean_value and not is_safe_preference_value(clean_value):
            raise ValueError("Sensitive information cannot be stored as a preference")
        if not clean_value:
            state = self.get_state(memory_manager)
            return {
                "success": True,
                "saved": False,
                "key": key,
                "value": "",
                "message": "已跳过该偏好设置。",
                **state,
            }

        if key in self.LIST_KEYS:
            self._append_list_preference(memory_manager, key, clean_value)
        else:
            memory_manager.long_term.save_preference(key, clean_value)

        state = self.get_state(memory_manager)
        label = self.DISPLAY_NAMES.get(key, key)
        return {
            "success": True,
            "saved": True,
            "key": key,
            "value": clean_value,
            "message": f"已记录：{label}为「{clean_value}」。",
            **state,
        }

    def _get_preferences(self, memory_manager) -> Dict[str, Any]:
        if not memory_manager:
            return {}
        prefs = memory_manager.long_term.get_preference()
        return prefs if isinstance(prefs, dict) else {}

    def _has_value(self, value: Any) -> bool:
        if isinstance(value, list):
            return any(self._has_value(item) for item in value)
        if value is None:
            return False
        return str(value).strip() not in self.EMPTY_VALUES

    def _clean_value(self, value: str) -> str:
        text = str(value or "").strip()
        return "" if text in self.EMPTY_VALUES else text

    def _append_list_preference(self, memory_manager, key: str, value: str) -> None:
        current = memory_manager.long_term.get_preference(key)
        values: List[str]
        if isinstance(current, list):
            values = [str(item) for item in current if self._has_value(item)]
        elif self._has_value(current):
            values = [str(current)]
        else:
            values = []

        if value not in values:
            values.append(value)
        memory_manager.long_term.save_preference(key, values)
