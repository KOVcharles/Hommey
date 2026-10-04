"""Opt-in, redacted snapshots of the exact arguments passed to the model SDK."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .store import safe_checkpoint
from .profiles import PROMPT_HASHES


def context_markdown(request):
    lines = ["# Model context", "", "SDK arguments, redacted; not a raw provider HTTP capture.", ""]
    for index, message in enumerate(request["messages"]):
        lines.extend([f"## {index:02d} · {message['role']}", ""])
        if message.get("tool_call_id"):
            lines.extend([f"Tool call: `{message['tool_call_id']}`", ""])
        content = message.get("content")
        if content:
            try:
                value = json.loads(content)
            except (ValueError, TypeError):
                lines.extend([str(content), ""])
            else:
                lines.extend(["```json", json.dumps(value, ensure_ascii=False, indent=2), "```", ""])
        if message.get("tool_calls"):
            lines.extend(["```json", json.dumps(message["tool_calls"], ensure_ascii=False, indent=2), "```", ""])
    lines.extend(["## Native tool schemas", "", "```json", json.dumps(request["tools"], ensure_ascii=False, indent=2), "```", ""])
    return "\n".join(lines)


def write_context_snapshot(directory, messages, tools, choice, *, role, round_number):
    """Called before dispatch; failures must not affect the business operation."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    request = safe_checkpoint({"messages": messages, "tools": tools, "tool_choice": choice})
    text = json.dumps(request, ensure_ascii=False, indent=2)
    manifest = {
        "format": "redacted-sdk-request.v1", "role": role or "main", "round": round_number,
        "request_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "messages": [{"index": i, "role": m["role"], "content_chars": len(m.get("content") or ""),
                      "tool_calls": [t["function"]["name"] for t in m.get("tool_calls", [])]}
                     for i, m in enumerate(messages)],
        "tools": [{"name": t["function"]["name"], "chars": len(json.dumps(t, ensure_ascii=False))} for t in tools],
        # Describe the rules loaded by this process, even if a prompt file has
        # since been edited on disk and the process has not restarted yet.
        "prompt_files": dict(sorted(PROMPT_HASHES.items())),
        "notes": "Character counts are not tokens. This snapshot excludes SDK/provider serialization and headers."
    }
    (directory / "request.json").write_text(text + "\n", encoding="utf-8")
    (directory / "context.md").write_text(context_markdown(request), encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
