"""Runtime-owned termination and recovery helpers; no model decides these rules."""
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


def is_continue(text):
    return re.sub(r"[\s，。！？,.!?]+", "", text) in {"继续", "请继续", "继续处理", "继续刚才的任务"}


def degraded_output(results, reason):
    messages = {
        "NO_PROGRESS": "本次部分步骤未能完成，已停止重复处理。以下保留可核实的结果。",
        "TURN_TIMEOUT": "本次处理已达到时间上限，以下保留已完成的结果。",
        "UPSTREAM_UNAVAILABLE": "部分服务暂时不可用，以下保留已完成的结果。",
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


def restore_results(checkpoint, version, is_valid):
    """Restore completed, committed evidence only; never replay historical writes."""
    if checkpoint.get("checkpoint_version") != 2:
        return {}
    restored = {}
    discarded = set(checkpoint.get("discarded_results", []))
    applied = set(checkpoint.get("applied_results", []))
    for key, raw in list(checkpoint.get("results", {}).items())[-12:]:
        try:
            result = SpecialistResult.model_validate(raw)
            if key in discarded or result.input_version != version or result.status not in {"success", "partial"}:
                continue
            proposal = result.role == "trip_context" and (result.data.get("trip") or result.data.get("trip_action", "update") != "update")
            proposal |= result.role == "memory" and bool(result.data.get("preferences"))
            if proposal and key not in applied:
                continue
            if is_valid(result):
                restored[key] = result.model_dump()
        except (ValueError, TypeError, KeyError):
            continue
    return restored
