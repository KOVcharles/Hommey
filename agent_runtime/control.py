"""Execution bounds, progress accounting, and bounded evidence retention."""
from __future__ import annotations

import re

from .contracts import Finish, SpecialistResult, ToolFailure
from .render import render
from .store import fingerprint


def failure(code, message, next_action="repair_arguments", fields=None, violations=None):
    return ToolFailure(code=code, message=message, next_action=next_action, fields=fields or [], violations=violations or []).wire()


def task_key(request, version):
    return fingerprint({"role": request.role, "version": version,
                        "dependencies": sorted(request.result_ids),
                        "task": re.sub(r"[\s，。！？,.!?]+", "", request.task).lower()})


def made_progress(call, value):
    """Count business facts, evidence or commits, not successful RPC envelopes."""
    if value.get("error") or value.get("reused") or value.get("repeated_observation"):
        return False
    if "_terminal" in value:
        return True
    name = call["name"]
    if name == "delegate":
        return bool(value.get("result_id")) and (
            value.get("committed") or value.get("status") in {"success", "partial"})
    if name == "apply_changes":
        return bool(value.get("applied"))
    if name in {"read_source", "read_result"}:
        return bool(value.get("id") or value.get("result_id"))
    if name in {"search_policy", "search_memory", "search_trains", "get_weather", "find_hotels", "search_commute"}:
        return bool(value.get("sources"))
    return False


def degraded_output(results, reason):
    messages = {
        "NO_PROGRESS": "本次部分步骤未能完成，已停止重复处理。以下保留可核实的结果。",
        "TURN_TIMEOUT": "本次处理已达到时间上限，以下保留已完成的结果。",
        "UPSTREAM_UNAVAILABLE": "部分服务暂时不可用，以下保留已完成的结果。",
        "SUPERVISOR_ROUND_LIMIT": "本次处理已达到步骤轮数上限，以下保留已完成的结果。",
    }
    notice = messages.get(reason, "本次处理已达到执行上限，以下保留已完成的结果。")
    if not results:
        notice += "当前没有可交付的查询结果；可以稍后继续。"
    # The notice is runtime-owned; unvalidated model summaries never reach here.
    result = render(Finish(kind="ask", question=notice), results)
    result.update(outcome="degraded", stop_reason=reason)
    section = result["answer_document"]["sections"][-1]
    section["title"] = "处理状态"
    from core.presentation.answer_document import AnswerDocument, render_plain_text
    document = AnswerDocument.model_validate(result["answer_document"])
    document.plain_text = render_plain_text(document)
    result["answer_document"] = document.model_dump(mode="json")
    result["response"] = document.plain_text
    return result


def restore_results(checkpoint, *, max_results=24, max_chars=350000):
    """Retain bounded same-session evidence, including stale/failed reports.

    A new turn must not inherit executable calls, failure quotas or UI steps.
    Committed IDs travel separately, so historical proposals cannot be replayed.
    Whole oldest results are evicted at the retention boundary, never truncated.
    """
    from .context_window import encoded_size
    restored, size = {}, 0
    for key, raw in reversed(list(checkpoint.get("results", {}).items())):
        try:
            result = SpecialistResult.model_validate(raw)
            if result.result_id != key:
                continue
            value = result.model_dump()
            cost = encoded_size(value)
            if len(restored) >= max_results:
                break
            if size + cost > max_chars:
                continue
            restored[key] = value
            size += cost
        except (ValueError, TypeError, KeyError):
            continue
    return dict(reversed(list(restored.items())))
