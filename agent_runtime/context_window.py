"""Bound model-visible context without dropping tool pairs or source provenance."""
from __future__ import annotations

import json


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, default=str))


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
            compact = {"skill": value["skill"], "notice": "旧业务指南已移出上下文，需要时再次读取。"}
        else:
            continue
        message["content"] = json.dumps(compact, ensure_ascii=False, default=str)
    return before - encoded_size(messages)
