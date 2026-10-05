"""Real incremental model consumption, strict tools and completed chat delivery."""

import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from agent_runtime.contracts import ToolRejected
from agent_runtime.engine import Supervisor
from agent_runtime.model_client import call_model, create_tool_model
from tests.test_supervisor_runtime import CONFIG, SCOPE, FakeServices, FakeStore, reply
from webui_new.manager import HommeyWebInstance


def text_snapshot(text):
    return {"content": [{"type": "text", "text": text}]}


@pytest.mark.asyncio
async def test_first_text_arrives_before_model_finishes_without_duplicate_snapshots():
    received, finish = asyncio.Event(), asyncio.Event()
    deltas = []

    async def model(*args, **kwargs):
        async def stream():
            yield {"content": [{"type": "thinking", "thinking": "private reasoning"}]}
            yield text_snapshot("准备")
            await finish.wait()
            yield text_snapshot("准备")  # SDK usage snapshots may repeat content.
            yield text_snapshot("准备报销材料。")

        return stream()

    async def on_text(delta):
        deltas.append(delta)
        received.set()

    task = asyncio.create_task(
        call_model(model, [], [], allow_text=True, on_text=on_text)
    )
    await asyncio.wait_for(received.wait(), 1)
    assert deltas == ["准备"] and not task.done()
    finish.set()
    result = await asyncio.wait_for(task, 1)
    assert deltas == ["准备", "报销材料。"]
    assert result.text == "".join(deltas) and result.calls == []


@pytest.mark.asyncio
async def test_tool_after_prose_retracts_draft_and_waits_for_complete_raw_arguments():
    partial, finish = asyncio.Event(), asyncio.Event()
    events = []

    async def model(*args, **kwargs):
        async def stream():
            yield text_snapshot("正在准备回答")
            yield {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "finish",
                        "input": {"kind": "ask"},
                        "raw_input": '{"kind":"ask","question":',
                    }
                ]
            }
            partial.set()
            await finish.wait()
            yield {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "finish",
                        "input": {},
                        "raw_input": '{"kind":"ask","question":"何时出发？"}',
                    }
                ]
            }

        return stream()

    async def on_text(delta):
        events.append(("text", delta))

    async def on_reset():
        events.append(("reset", None))

    task = asyncio.create_task(
        call_model(model, [], [], allow_text=True, on_text=on_text, on_reset=on_reset)
    )
    await asyncio.wait_for(partial.wait(), 1)
    assert not task.done() and events == [("text", "正在准备回答"), ("reset", None)]
    finish.set()
    result = await asyncio.wait_for(task, 1)
    assert result.calls[0]["arguments"] == {"kind": "ask", "question": "何时出发？"}
    assert len(events) == 2  # No tool JSON or child report is shown as prose.


@pytest.mark.asyncio
async def test_stream_disconnect_retracts_partial_prose():
    events = []

    async def model(*args, **kwargs):
        async def stream():
            yield text_snapshot("尚未完成的回答")
            raise OSError("provider disconnected")

        return stream()

    async def on_text(delta):
        events.append(delta)

    async def on_reset():
        events.append("reset")

    with pytest.raises(OSError):
        await call_model(
            model, [], [], allow_text=True, on_text=on_text, on_reset=on_reset
        )
    assert events == ["尚未完成的回答", "reset"]


@pytest.mark.asyncio
async def test_cancel_while_handling_delta_closes_provider_generator():
    entered, closed = asyncio.Event(), asyncio.Event()

    async def model(*args, **kwargs):
        async def stream():
            try:
                yield text_snapshot("第一段")
                await asyncio.sleep(10)
            finally:
                closed.set()

        return stream()

    async def on_text(delta):
        entered.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(
        call_model(model, [], [], allow_text=True, on_text=on_text)
    )
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_supervisor_streams_prose_then_persists_exact_final_text_and_replays():
    received, finish = asyncio.Event(), asyncio.Event()
    events, calls = [], []

    async def model(*args, **kwargs):
        calls.append(True)

        async def stream():
            yield text_snapshot("先准备发票，")
            await finish.wait()
            yield text_snapshot("先准备发票，再核对适用制度。")

        return stream()

    async def progress(event):
        events.append(event)
        if event["type"] == "chunk":
            received.set()

    services = FakeServices()
    store = FakeStore(services)
    runtime = Supervisor(model, services, store, CONFIG)
    task = asyncio.create_task(
        runtime.run(SCOPE, "解释差旅报销材料怎么准备", progress=progress)
    )
    await asyncio.wait_for(received.wait(), 2)
    row = store.rows[(SCOPE.user_id, SCOPE.request_id)]
    assert not task.done() and row["response"] is None and store.writes == 0
    finish.set()
    result = await asyncio.wait_for(task, 2)
    assert result["response"] == "".join(
        e["text"] for e in events if e["type"] == "chunk"
    )
    assert row["response"]["response"] == result["response"]
    replay = await runtime.run(SCOPE, "解释差旅报销材料怎么准备")
    assert replay["idempotent_replay"] and replay["response"] == result["response"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_manager_delivers_live_deltas_without_repeating_final_answer(monkeypatch):
    instance = HommeyWebInstance("stream-user")
    received, finish = asyncio.Event(), asyncio.Event()
    events = []

    async def process(message, *, progress_callback, **kwargs):
        await progress_callback({"type": "chunk", "text": "第一段"})
        await finish.wait()
        await progress_callback({"type": "chunk", "text": "第二段"})
        return {"response": "第一段第二段"}

    monkeypatch.setattr(instance, "process_message", process)

    async def consume():
        async for event in instance.stream_message("报销材料"):
            events.append(event)
            if event["type"] == "chunk":
                received.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(received.wait(), 1)
    assert not task.done() and events[-1]["text"] == "第一段"
    finish.set()
    await asyncio.wait_for(task, 1)
    assert [e["text"] for e in events if e["type"] == "chunk"] == ["第一段", "第二段"]
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"response": "已核实的结果", "answer_document": {"sections": []}},
        {
            "response": "请补充信息",
            "presentation_document": {"type": "information_request"},
        },
        {"response": "本次未取得可核实的资料"},
    ],
)
async def test_manager_replaces_draft_with_validated_card_or_fallback(
    monkeypatch, result
):
    instance = HommeyWebInstance("stream-user")

    async def process(message, *, progress_callback, **kwargs):
        await progress_callback({"type": "chunk", "text": "未完成草稿"})
        return result

    monkeypatch.setattr(instance, "process_message", process)
    events = [event async for event in instance.stream_message("报销材料")]
    assert [event["type"] for event in events[:3]] == [
        "status",
        "chunk",
        "response_reset",
    ]
    assert events[-1]["type"] == "done"
    final = events[-2]
    expected = (
        "answer_document"
        if result.get("answer_document")
        else "presentation_document" if result.get("presentation_document") else "chunk"
    )
    assert final["type"] == expected
    if expected == "chunk":
        assert final["text"] == result["response"]


@pytest.mark.asyncio
async def test_closing_chat_stream_cancels_inflight_model_request(monkeypatch):
    instance = HommeyWebInstance("stream-user")
    closed = asyncio.Event()

    async def process(message, *, progress_callback, **kwargs):
        try:
            await progress_callback({"type": "chunk", "text": "第一段"})
            await asyncio.sleep(10)
        finally:
            closed.set()

    monkeypatch.setattr(instance, "process_message", process)
    stream = instance.stream_message("报销材料")
    assert (await anext(stream))["type"] == "status"
    assert (await anext(stream))["type"] == "chunk"
    await stream.aclose()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_installed_sdk_stream_retains_raw_invalid_arguments_without_repair_authorization():
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        frames = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call",
                                    "type": "function",
                                    "function": {
                                        "name": "finish",
                                        "arguments": '{"kind":"ask","question":"何时',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": "出发？"}}
                            ]
                        },
                        "finish_reason": "length",
                    }
                ]
            },
        ]
        body = (
            "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
            + "data: [DONE]\n\n"
        )
        return httpx.Response(
            200, text=body, headers={"Content-Type": "text/event-stream"}
        )

    model = create_tool_model(
        {
            "model_name": "test",
            "api_key": "test-key",
            "base_url": "http://provider.test/v1",
        },
        {},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        model.client = AsyncOpenAI(
            api_key="test-key", base_url="http://provider.test/v1", http_client=client
        )
        with pytest.raises(ToolRejected):
            await call_model(model, [{"role": "user", "content": "报销材料"}], [])
    assert requests[0]["stream"] is True
    assert model.stream_tool_parsing is False
