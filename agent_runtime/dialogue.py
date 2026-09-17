"""Bind replies to the last displayed question in the same trip version."""
from __future__ import annotations

import re

from pydantic import ValidationError

from .contracts import Finish, PendingInput
from .render import render


CLARIFICATION = "我还不太确定你的意思。你是想查询差旅政策、规划行程，还是查询交通天气？"


def clarification_output(question=CLARIFICATION, pending_input=None):
    output = render(Finish(kind="clarify", question=question, pending_input=pending_input), [])
    output["outcome"] = "waiting_input"
    if pending_input:
        output["_pending_input"] = {"question": question, "input": pending_input.model_dump()}
    return output


def previous_question(previous, version):
    if not previous or previous.get("status") != "completed":
        return None
    checkpoint = previous.get("checkpoint") or {}
    if checkpoint.get("control", {}).get("outcome") != "waiting_input":
        return None
    pending = checkpoint.get("pending_input")
    if not isinstance(pending, dict) or pending.get("trip_version") != version or not pending.get("question"):
        return None
    try:
        definition = PendingInput.model_validate(pending["input"])
    except (ValidationError, KeyError, TypeError):
        return None
    return {**pending, "input": definition.model_dump()}


def resolve_reply(text, pending):
    """Return a binding, never infer one from the mere existence of a trip."""
    if not pending:
        return None
    spec = PendingInput.model_validate(pending["input"])
    value = text.strip()
    if spec.choices:
        if re.fullmatch(r"[0-9]{1,2}", value) and 1 <= int(value) <= len(spec.choices):
            return {"choice": spec.choices[int(value) - 1], "quote": text}
        if value in spec.choices:
            return {"choice": value, "quote": text}
        return None
    if spec.field in {"duration_days", "trip_length"}:
        match = re.fullmatch(r"([1-9][0-9]?)\s*天?", value)
        if match and int(match[1]) <= 90:
            return {"field": "duration_days", "value": int(match[1]), "quote": text}
    # A number cannot be silently treated as a city/purpose/date.
    # Longer utterances retain full semantic routing (including task switches).
    if re.fullmatch(r"[\u4e00-\u9fff]{1,2}", value):
        return {"field": spec.field, "value": value, "quote": text}
    return None


def record_question(state, output):
    pending = output.pop("_pending_input", None)
    if output.get("outcome") != "waiting_input":
        state["pending_input"] = None
        return
    document = output.get("presentation_document") or {}
    if document.get("type") == "trip_intake" and document.get("missing_required"):
        field = document["missing_required"][0]
        pending = {"question": document["next_question"], "input": PendingInput(field=field["key"]).model_dump()}
    state["pending_input"] = {**pending, "trip_version": state["version"]} if pending else None


def can_show_intake(state, results):
    """A result-based card requires usable, committed trip facts, not a role name."""
    return bool(results) and all(
        r.role == "trip_context" and r.status in {"success", "partial", "needs_input"}
        and r.data.get("trip_action", "update") != "cancel" and bool(r.data.get("trip"))
        and r.result_id in state.get("applied_results", []) for r in results
    )
