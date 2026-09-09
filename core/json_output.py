"""JSON text extraction for offline evaluation (not the native tool protocol)."""
import json


def clean_json_text(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def parse_json_object(text: str) -> dict:
    cleaned = clean_json_text(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as first_error:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or start >= end:
            sample = cleaned[:300]
            raise ValueError(f"No JSON object found in model response: {sample}") from first_error

        snippet = cleaned[start:end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError as second_error:
            sample = snippet[:300]
            raise ValueError(f"Failed to parse JSON object: {second_error}. Sample: {sample}") from second_error
