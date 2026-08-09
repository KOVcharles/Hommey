import asyncio

import pytest

from core.intent_result import parse_json_object, validate_intent_result
from core.llm_response import extract_text_from_response


class TextResponse:
    text = '{"ok": true}'


class ContentResponse:
    content = [{"type": "text", "text": '{"ok": true}'}]


async def chunked_response():
    yield '{"a":'
    yield " 1"
    yield "}"


async def cumulative_chunked_response():
    yield "{"
    yield '{"a":'
    yield '{"a": 1}'


def _valid_intent_result(**overrides):
    data = {
        "routing": {
            "intent": "unclear",
            "confidence": 0.9,
            "reason": "test",
            "should_call_skill": False,
        },
        "reasoning": "test",
        "intents": [
            {
                "type": "unclear",
                "confidence": 0.9,
                "description": "",
                "reason": "test",
                "should_call_skill": False,
            }
        ],
        "key_entities": {},
        "rewritten_query": "test",
    }
    data.update(overrides)
    return data


def test_extract_text_from_response_shapes():
    assert asyncio.run(extract_text_from_response(None)) == ""
    assert asyncio.run(extract_text_from_response("plain")) == "plain"
    assert asyncio.run(extract_text_from_response({"content": "dict"})) == "dict"
    assert asyncio.run(extract_text_from_response(TextResponse())) == '{"ok": true}'
    assert asyncio.run(extract_text_from_response(ContentResponse())) == '{"ok": true}'
    assert asyncio.run(extract_text_from_response(chunked_response())) == '{"a": 1}'
    assert asyncio.run(extract_text_from_response(cumulative_chunked_response())) == '{"a": 1}'


def test_parse_json_object_handles_common_wrappers():
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('解释 {"a": 1} 结尾') == {"a": 1}


def test_parse_json_object_rejects_invalid_json():
    with pytest.raises(ValueError):
        parse_json_object("not json")


def test_validate_intent_result_accepts_valid_payload():
    result = validate_intent_result(_valid_intent_result())

    assert result["routing"]["intent"] == "unclear"
    assert result["routing"]["should_call_skill"] is False


def test_validate_intent_result_rejects_missing_routing():
    data = _valid_intent_result()
    data.pop("routing")

    with pytest.raises(ValueError):
        validate_intent_result(data)


def test_validate_intent_result_rejects_invalid_confidence():
    data = _valid_intent_result(
        routing={
            "intent": "unclear",
            "confidence": 1.5,
            "reason": "bad",
            "should_call_skill": False,
        }
    )

    with pytest.raises(ValueError):
        validate_intent_result(data)
