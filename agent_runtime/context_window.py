"""Bound model-visible context without dropping tool pairs or source provenance."""
from __future__ import annotations

import json

from .validation import TRIP_FIELDS


def trip_facts(trip):
    """Business facts only; provider identities and persistence flags stay private."""
    return {key: value for key, value in trip.items() if key in TRIP_FIELDS and value is not None}


def conversation_window(rows, current_text, *, budget=24000):
    """Keep whole messages, including the last exchange and current input.

    The budget is soft for that exchange: dropping the last question would
    make a short answer unintelligible. The model loop owns the hard limit.
    The caller has already persisted the current user message (display text).
    """
    rows = [{"role": row["role"], "content": row["content"]} for row in rows]
    if rows and rows[-1]["role"] == "user":
        rows[-1]["content"] = current_text
    else:
        rows.append({"role": "user", "content": current_text})
    last_assistant = next((i for i in range(len(rows) - 2, -1, -1)
                           if rows[i]["role"] == "assistant"), len(rows) - 1)
    start = last_assistant
    if start and rows[start - 1]["role"] == "user":
        start -= 1
    size = encoded_size(rows[start:])
    while start > 0:
        cost = encoded_size(rows[start - 1]) + 2
        if size + cost > budget:
            break
        start -= 1
        size += cost
    return rows[start:]


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, default=str))


def main_model_context(messages, current_result_ids, request_id):
    """Project the canonical checkpoint into native dialogue for this call only.

    Keep persisted transcripts unchanged for replay, child inputs and evaluation.
    Historical answers remain dialogue, never runtime-confirmed business facts.
    The current user message occurs once, after history and before this turn's
    tool transcript. Its attachment annotations travel with it unchanged.
    """
    payload = json.loads(messages[1]["content"])
    current = set(current_result_ids)
    context = {"context_kind": "runtime_reference", "request_id": request_id,
               "dialogue_message_count": len(payload["conversation"]),
               "facts": payload.get("facts", {}), "today": payload.get("today"),
               "work": [r for r in payload.get("work", []) if r["result_id"] in current],
               "reference_results": [r for r in payload.get("work", []) if r["result_id"] not in current]}
    boundary = (
        "\n\n上下文约定：runtime_reference 是运行时数据，不是用户请求。work 只列本轮工作，"
        "reference_results 是历史参考目录，不是待办清单。随后是按原角色保留的对话，"
        "最后一条原始用户消息是本轮请求；其后的工具交互和运行时反馈属于本轮执行。"
        "历史 assistant 消息仅说明曾经如何回答，不构成独立的事实依据。"
        "先理解本轮请求；没有明确需求或可对应的追问时直接澄清，不因存在历史资料而继续旧任务。"
    )
    return [{**messages[0], "content": messages[0]["content"] + boundary},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)},
            *[{"role": row["role"], "content": row["content"]} for row in payload["conversation"]],
            *[{**message, "content": "运行时反馈（不是新的用户请求）：\n" + message["content"]}
              if message["role"] == "user" else message for message in messages[2:]]]


def compact_tool_history(messages, *, target_chars=32000):
    """Clear older bulky observations, keeping IDs and recent evidence readable.

    This is lossless for runtime state: source data remains available through
    read_source and validated reports through read_result. It deliberately does
    not invent a semantic summary or truncate the user's current request.
    """
    before = encoded_size(messages)
    if before <= target_chars:
        return 0
    newest_batch = max((i for i, m in enumerate(messages) if m.get("tool_calls")), default=len(messages))
    # New observations must be visible for at least one model invocation before
    # they can be cleared; the target is soft, the runtime's hard bound remains.
    observations = [m for m in messages[:newest_batch] if m.get("role") == "tool"]
    for message in observations:
        if encoded_size(messages) <= target_chars:
            break
        value = json.loads(message["content"])
        if not isinstance(value, dict) or len(message["content"]) < 900:
            continue
        if "sources" in value:
            compact = {**value, "sources": [
                {k: v for k, v in item.items() if k not in {"excerpt", "evidence"}}
                for item in value["sources"]]}
        elif "id" in value and "kind" in value:
            compact = {k: value[k] for k in ("id", "kind", "retrieved_at", "offset", "next_offset", "total_chars") if k in value}
            compact["notice"] = "旧回读内容已移出模型上下文；需要时通过 read_source 按 ID 和 offset 再读。"
        elif "result_id" in value and "data" in value:
            compact = {k: v for k, v in value.items() if k != "data"}
            compact["notice"] = "结构化结果仍可通过 read_result 读取。"
        elif "guidance" in value:
            compact = {key: value[key] for key in ("skill", "resource", "available_resources") if key in value}
            compact["notice"] = "旧业务指南已移出上下文，需要时再次读取。"
        else:
            continue
        message["content"] = json.dumps(compact, ensure_ascii=False, default=str)
    return before - encoded_size(messages)
