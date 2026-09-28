"""Bounded, durable request/outcome fingerprints for tool-loop detection."""
from __future__ import annotations

from .store import fingerprint, safe_checkpoint


MAX_OBSERVATIONS = 64


def record_tool_outcome(state, name, request_hash, value, *, terminal_tool):
    """Mark repeated observations, without caching IO or hiding readable evidence.

    Called once per executed call, before its output and checkpoint are saved.
    Replaying a saved output must bypass this function. The ledger belongs to
    one agent in one user turn, independently of compactable model messages.
    """
    if value.get("error") or "_terminal" in value:
        return value
    # Ignore our own annotations; they must never manufacture new progress.
    observed = {k: v for k, v in value.items()
                if k not in {"reused", "repeated_observation", "loop_warning"}}
    if name == "read_source":
        # Retrieval time and paging advice are envelope metadata. Preserve the
        # actual data, source identity, page boundaries and freshness flags.
        observed.pop("retrieved_at", None)
        observed.pop("notice", None)
    elif name == "read_result":
        observed["sources"] = [{k: v for k, v in source.items() if k != "retrieved_at"}
                               for source in observed.get("sources", [])]
    # Hash the same sanitized representation that survives durable recovery.
    result_hash = fingerprint(safe_checkpoint(observed))
    # Hashes are keys so checkpoint text redaction cannot mistake digits in a
    # digest for personal data. Values are counts, never raw arguments/results.
    key = request_hash + ":" + result_hash
    history = state.setdefault("tool_observations", {})
    count = history.get(key, {}).get("count", 0) + 1
    sequence = max((entry["last_seen"] for entry in history.values()), default=0) + 1
    history[key] = {"count": count, "last_seen": sequence}
    while len(history) > MAX_OBSERVATIONS:
        # JSONB reorders object keys; eviction must not depend on insertion order.
        history.pop(min(history, key=lambda key: history[key]["last_seen"]))
    if count == 1:
        return value
    return {**value, "repeated_observation": True,
            "loop_warning": "此调用返回的内容与本轮已取得的信息相同，未增加新信息。"
                "如仍有具体缺项，请针对缺项处理；证据够用时调用 " + terminal_tool + " 交付已核实结果，必要时说明未知项。"}
