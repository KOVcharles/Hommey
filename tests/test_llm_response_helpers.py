import asyncio

import pytest

from core.json_output import parse_json_object
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
