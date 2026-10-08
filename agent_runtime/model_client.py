"""Normalize native tool calls, including streamed deltas. Never parse prose commands."""
from __future__ import annotations

import json
from collections.abc import AsyncIterable
from dataclasses import dataclass
from typing import Any
from .contracts import ToolRejected


@dataclass
class ModelReply:
    calls: list[dict]
    text: str = ""


def _blocks(response: Any) -> list[dict]:
    content = response.get("content", []) if isinstance(response, dict) else getattr(response, "content", [])
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [block for block in (content or []) if isinstance(block, dict)]


async def call_model(model, messages: list[dict], tools: list[dict], *, allow_text=False, context_capture=None,
                     on_text=None, on_reset=None) -> ModelReply:
    # AgentScope accepts a tool name and formats the provider's forced function
    # choice. This matters for the final extraction round: "required" alone
    # was sometimes answered as prose by the configured compatible endpoint.
    choice = ("auto" if tools else "none") if allow_text else "required"
    if not allow_text and len(tools) == 1:
        choice = tools[0]["function"]["name"]
    if context_capture:
        context_capture(messages, tools, choice)
    response = await model(messages, tools=tools, tool_choice=choice)
    # Tool-enabled text can be a preamble whose tool call arrives much later.
    # Only the explicit, tool-disabled answer phase may publish text deltas.
    if tools:
        on_text = None
    # Native tool arguments remain buffered until strict validation completes.
    visible_text, has_tools = "", False
    stream = response if isinstance(response, AsyncIterable) else None
    try:
        if stream is not None:
            last = None
            async for chunk in stream:
                last = chunk
                blocks = _blocks(chunk)
                if any(block.get("type") == "tool_use" for block in blocks):
                    has_tools = True
                    if visible_text and on_reset:
                        await on_reset()
                    visible_text = ""
                if allow_text and on_text and not has_tools:
                    text = "".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text")
                    if not text.startswith(visible_text):
                        raise ToolRejected("模型正文流不连续，请重新生成完整回复")
                    delta = text[len(visible_text):]
                    if delta:
                        await on_text(delta)
                    visible_text = text
            response = last
        return await _parse_reply(response, allow_text, on_text, visible_text)
    except Exception:
        if visible_text and on_reset:
            await on_reset()
        raise
    finally:
        # Cancelling a turn must close the provider connection, including when
        # cancellation happens while the consumer is handling a text delta.
        if stream is not None and getattr(stream, "aclose", None):
            await stream.aclose()


async def _parse_reply(response, allow_text, on_text, visible_text):
    calls, text = [], []
    for block in _blocks(response):
        if block.get("type") == "tool_use":
            # Prefer the un-repaired payload when supplied by AgentScope. A
            # repaired partial JSON object must never authorize an operation.
            args = block.get("raw_input") or block.get("input", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError as exc:
                    raise ToolRejected("工具参数不是完整 JSON，请重新生成原生工具调用") from exc
            if not isinstance(args, dict):
                raise ToolRejected("工具参数必须是 JSON 对象")
            calls.append({"id": str(block.get("id") or ""), "name": str(block.get("name") or ""), "arguments": args})
        elif block.get("type") == "text":
            text.append(str(block.get("text") or ""))
    if any(not c["id"] or not c["name"] for c in calls) or len({c["id"] for c in calls}) != len(calls):
        raise ToolRejected("工具调用 ID 缺失或重复")
    answer = "".join(text)
    if allow_text and on_text and not calls and answer and not visible_text:
        await on_text(answer)
    return ModelReply(calls, answer)


def create_tool_model(config, generate_kwargs, timeout=60):
    """Stream prose while retaining raw, unparsed tool arguments for validation."""
    from agentscope.model import OpenAIChatModel

    class StrictToolModel(OpenAIChatModel):
        def _parse_openai_completion_response(self, start_datetime, response, structured_model=None):
            parsed = super()._parse_openai_completion_response(start_datetime, response, structured_model)
            raw_calls = {call.id: call.function.arguments for choice in response.choices for call in (choice.message.tool_calls or [])}
            for block in parsed.content:
                if block.get("type") == "tool_use":
                    block["raw_input"] = raw_calls[block["id"]]
            return parsed

    return StrictToolModel(model_name=config["model_name"], api_key=config["api_key"], stream=True,
        stream_tool_parsing=False,
        client_kwargs={"base_url": config["base_url"], "timeout": float(timeout), "max_retries": 0},
        generate_kwargs=generate_kwargs)


def assistant_message(calls: list[dict], text: str = "") -> dict:
    message = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = [
            {"id": call["id"], "type": "function", "function": {
                "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False),
            }} for call in calls
        ]
    return message


def tool_message(call: dict, output: Any) -> dict:
    return {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(output, ensure_ascii=False, default=str)}
