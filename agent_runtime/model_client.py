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


async def call_model(model, messages: list[dict], tools: list[dict]) -> ModelReply:
    # AgentScope accepts a tool name and formats the provider's forced function
    # choice. This matters for the final extraction round: "required" alone
    # was sometimes answered as prose by the configured compatible endpoint.
    choice = tools[0]["function"]["name"] if len(tools) == 1 else "required"
    response = await model(messages, tools=tools, tool_choice=choice)
    # AgentScope streaming ChatResponse contains cumulative tool blocks. Consume
    # the complete final snapshot before validating or running any operation.
    if isinstance(response, AsyncIterable):
        last = None
        async for chunk in response:
            last = chunk
        response = last
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
    return ModelReply(calls, "".join(text))


def create_tool_model(config, generate_kwargs, timeout=60):
    """Buffer native tool responses and retain unmodified arguments for validation.

    The UI streams task progress, not partial tool arguments. AgentScope's
    default nonstream parser repairs JSON and drops raw_input; restore the
    original provider arguments so repaired/truncated calls cannot authorize
    mutations. Avoid reparsing every token delta for large extraction reports.
    """
    from agentscope.model import OpenAIChatModel

    class StrictToolModel(OpenAIChatModel):
        def _parse_openai_completion_response(self, start_datetime, response, structured_model=None):
            parsed = super()._parse_openai_completion_response(start_datetime, response, structured_model)
            raw_calls = {call.id: call.function.arguments for choice in response.choices for call in (choice.message.tool_calls or [])}
            for block in parsed.content:
                if block.get("type") == "tool_use":
                    block["raw_input"] = raw_calls[block["id"]]
            return parsed

    return StrictToolModel(model_name=config["model_name"], api_key=config["api_key"], stream=False,
        client_kwargs={"base_url": config["base_url"], "timeout": float(timeout), "max_retries": 0},
        generate_kwargs=generate_kwargs)


def assistant_message(calls: list[dict]) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call["id"], "type": "function", "function": {
            "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False),
        }} for call in calls
    ]}


def tool_message(call: dict, output: Any) -> dict:
    return {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(output, ensure_ascii=False, default=str)}
