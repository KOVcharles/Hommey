"""Verify load-test measurements without calling Hommey, a database or an LLM."""

import asyncio
import json

import httpx
import pytest

from scripts.load_test import (
    Sample,
    api_request,
    chat_request,
    main,
    parser,
    run_arrivals,
    run_chat,
    summarize,
)


class EventStream(httpx.AsyncByteStream):
    def __init__(self, events, delay=0):
        self.events = events
        self.delay = delay

    async def __aiter__(self):
        for event in self.events:
            await asyncio.sleep(self.delay)
            yield (json.dumps(event) + "\n").encode()


@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"code": "FAIL"}},
        {"sessions": [], "success": False},
        [],
        {"sessions": "wrong"},
    ],
)
def test_api_does_not_count_http_200_without_valid_business_response(payload):
    async def scenario():
        async with httpx.AsyncClient(
            base_url="http://test",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        ) as client:
            sample = await api_request(client, "/api/1/sessions", "sessions", 1)
            assert not sample.ok

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "events,expected",
    [
        (
            [{"type": "chunk", "text": "hi"}, {"type": "error", "code": "MODEL_LIMIT"}],
            "MODEL_LIMIT",
        ),
        ([{"type": "chunk", "text": "hi"}], "MISSING_DONE"),
        (
            [{"type": "status"}, {"type": "done", "outcome": "completed"}],
            "EMPTY_RESPONSE",
        ),
        (
            [{"type": "chunk", "text": "hi"}, {"type": "done", "outcome": "degraded"}],
            "OUTCOME_DEGRADED",
        ),
        (
            [
                {"type": "chunk", "text": "hi"},
                {"type": "done", "outcome": "waiting_input"},
            ],
            "OUTCOME_WAITING_INPUT",
        ),
        (
            [
                {"type": "chunk", "text": "draft"},
                {"type": "response_reset"},
                {"type": "done", "outcome": "completed"},
            ],
            "EMPTY_RESPONSE",
        ),
    ],
)
def test_stream_rejects_incomplete_error_or_noncompleted_answers(events, expected):
    async def scenario():
        async with httpx.AsyncClient(
            base_url="http://test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=EventStream(events))
            ),
        ) as client:
            sample = await chat_request(client, "1", "s", "question", 1)
            assert not sample.ok
            assert sample.status == 200
            assert sample.error == expected

    asyncio.run(scenario())


def test_first_text_ignores_status_and_whitespace_chunks():
    async def scenario():
        events = [
            {"type": "status"},
            {"type": "chunk", "text": " "},
            {"type": "chunk", "text": "answer"},
            {"type": "done", "outcome": "completed"},
        ]
        async with httpx.AsyncClient(
            base_url="http://test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=EventStream(events, delay=0.01))
            ),
        ) as client:
            sample = await chat_request(client, "1", "s", "question", 1)
            assert sample.ok
            assert sample.first_text_ms >= 25
            assert sample.first_text_ms == sample.first_content_ms
            assert sample.elapsed_ms > sample.first_text_ms

    asyncio.run(scenario())


def test_card_has_first_content_but_no_first_text():
    async def scenario():
        events = [
            {"type": "answer_document", "document": {"type": "answer"}},
            {"type": "done", "outcome": "completed"},
        ]
        async with httpx.AsyncClient(
            base_url="http://test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=EventStream(events))
            ),
        ) as client:
            sample = await chat_request(client, "1", "s", "question", 1)
            assert sample.ok
            assert sample.first_text_ms is None
            assert sample.first_content_ms is not None

    asyncio.run(scenario())


def test_total_timeout_stops_a_stream_that_does_not_finish():
    async def scenario():
        async with httpx.AsyncClient(
            base_url="http://test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, stream=EventStream([{"type": "status"}], delay=1)
                )
            ),
        ) as client:
            sample = await chat_request(client, "1", "s", "question", 0.01)
            assert not sample.ok
            assert sample.error == "TimeoutError"

    asyncio.run(scenario())


def test_arrival_test_bounds_inflight_and_reports_unoffered_load():
    async def scenario():
        active = maximum = 0

        async def handler(_):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.06)
            active -= 1
            return httpx.Response(200, json={"sessions": []})

        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        ) as client:
            report = await run_arrivals(
                client, "/api/1/sessions", "sessions", 50, 0.2, 1, 1
            )
        assert maximum == 1
        assert report["scheduled"] == 10
        assert report["attempted"] + report["dropped_before_dispatch"] == 10
        assert report["dropped_before_dispatch"] > 0
        assert report["dispatched_per_s"] < report["target_qps"]
        assert report["elapsed_s"] >= report["window_s"]

    asyncio.run(scenario())


def test_chat_uses_unique_sessions_and_matching_request_ids():
    async def scenario():
        created, used = [], []

        async def handler(request):
            if request.url.path.endswith("/sessions"):
                session = f"test-session-{len(created)}"
                created.append(session)
                return httpx.Response(200, json={"session_id": session})
            assert len(created) == 4  # Setup is complete before the measured stage.
            payload = json.loads(request.content)
            assert request.headers["X-Request-ID"] == payload["client_request_id"]
            used.append(payload["session_id"])
            return httpx.Response(
                200,
                stream=EventStream(
                    [
                        {"type": "chunk", "text": "answer"},
                        {"type": "done", "outcome": "completed"},
                    ],
                    delay=0.001,
                ),
            )

        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        ) as client:
            report = await run_chat(client, "1", 2, 2, "question", 1)
        assert len(set(used)) == 4
        assert report["successful"] == 4
        assert report["outcomes"] == {"completed": 4}

    asyncio.run(scenario())


def test_summary_separates_failure_latency_from_success_latency():
    report = summarize(
        [Sample(ok=True, elapsed_ms=20), Sample(error="HTTP_500", elapsed_ms=1000)], 2
    )
    assert report["successful_per_s_including_drain"] == 0.5
    assert report["latency_successful_ms"]["p95"] == 20
    assert report["latency_all_ms"]["p95"] == 1000
    assert report["unsuccessful_rate"] == 0.5


def test_cli_report_excludes_credentials_bodies_and_warmup(tmp_path, monkeypatch):
    real_client = httpx.AsyncClient
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/auth/login":
            assert json.loads(request.content)["password"] == "SECRET_PASSWORD"
            return httpx.Response(200, json={"access_token": "SECRET_TOKEN"})
        assert request.headers["Authorization"] == "Bearer SECRET_TOKEN"
        if request.url.path == "/api/me":
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(200, json={"sessions": [{"title": "PRIVATE_BODY"}]})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr("getpass.getpass", lambda _: "SECRET_PASSWORD")
    output = tmp_path / "report.json"
    args = parser().parse_args(
        [
            "--email",
            "test@example.com",
            "--output",
            str(output),
            "api",
            "--qps",
            "2",
            "--duration",
            "1",
            "--warmup",
            "1",
        ]
    )
    assert asyncio.run(main(args)) == 0
    text = output.read_text(encoding="utf-8")
    for private in (
        "SECRET_PASSWORD",
        "SECRET_TOKEN",
        "PRIVATE_BODY",
        "test@example.com",
    ):
        assert private not in text
    report = json.loads(text)
    assert report["attempted"] == 2
    assert report["successful"] == 2
    assert calls.count("/api/1/sessions") == 5  # Smoke + two warmup + two measured.
