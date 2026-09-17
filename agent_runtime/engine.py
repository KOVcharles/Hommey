"""One bounded supervisor loop, six isolated leaf profiles, durable tool boundaries."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import re
import time
from uuid import uuid4

from pydantic import ValidationError

from core.execution_budget import ExecutionLimitExceeded, consume_agent_call
from core.intent_guard import guard_user_input
from core.trip_intake import beijing_today, evaluate_trip_intake
from utils.io_executor import run_blocking
from .contracts import (
    ApplyChanges, Delegate, Finish, ReadResult, ReadSkill, Report, RuntimeStopped,
    SpecialistResult, ToolRejected, PolicyReport, REPORT_MODELS, WorkItem, schema,
    Empty, PendingInput,
)
from .model_client import assistant_message, call_model, tool_message
from .profiles import BASE_RULES, MAIN_RULES, POLICY_QUERY_RULES, PROFILES
from .render import REFUSAL, render
from .services import SourceScope, TOOLS, tool_schemas
from .store import fingerprint, trip_version
from .validation import validate_report, validated_changes
from .context_window import compact_tool_history, encoded_size
from .control import failure, task_key, is_continue, degraded_output, restore_results, made_progress
from .dialogue import clarification_output, previous_question, resolve_reply, record_question, can_show_intake
from .intake_submission import parse_intake_submission, parse_trip_entry
from . import execution_plan
from .fast_routes import weather_policy_tasks


logger = logging.getLogger(__name__)
MAIN_TOOLS = [
    schema("delegate", "委派一个独立专业任务；result_ids 只填已完成的依赖，可同轮并行委派", Delegate),
    schema("read_result", "按 ID 查看专业结果结构和证据引用，不读取原始检索日志", ReadResult),
    schema("discard_result", "丢弃无效或过时结果，解除其尚未提交的变更；已经提交的写入不会撤销", ReadResult),
    schema("read_skill", "读取允许的差旅 Skill 业务指南", ReadSkill),
    schema("apply_changes", "提交有本轮用户原文依据的行程/偏好变更；只接受专业结果 ID", ApplyChanges),
    schema("finish", "结束本轮：answer 选择有据结果；ask 任务缺资料；clarify 意图不明且不选结果；refuse 明确超范围。编号选项用 pending_input.choices，由运行时展示和绑定回复", Finish),
    schema("prepare_trip_options", "完整差旅交付：验证目的地内会场，查询真实交通酒店及适用制度，调用规划角色后生成含每日安排的卡片；只用于安排出差，不用于单独制度/记忆查询", Empty),
]
MODELS = {"delegate": Delegate, "read_result": ReadResult, "discard_result": ReadResult, "read_skill": ReadSkill, "apply_changes": ApplyChanges, "finish": Finish, "prepare_trip_options": Empty}


def is_intake_entry(text):
    """Only unambiguous empty intake requests; mixed questions keep agent routing."""
    normalized = re.sub(r"[\s，。！？,.!?]+", "", text).lower()
    return normalized in {"出差", "chuchai", "我要出差", "我想出差", "我要去出差",
                          "安排出差", "帮我安排出差", "我要安排出差", "出差计划", "出差规划"}


def intake_output(trip, home_location="", *, saved=False):
    from core.presentation.trip_intake_document import build_trip_intake_document
    document = build_trip_intake_document(trip, home_location=home_location, saved=saved).model_dump(mode="json")
    return {"response": document["plain_text"], "answer_document": None, "presentation_document": document}


def is_direct_policy_query(text):
    """Conservative single-purpose standard lookup; mixed/ambiguous tasks stay agentic."""
    return bool(re.fullmatch(
        r"\s*(?:(?:我(?:要|准备|计划)?|要|准备|计划)?去[\u4e00-\u9fff]{2,12}出差[，,\s]*)?"
        r"(?:请|帮我)?(?:看看|看一下|查查|查一下|查询|了解一下)"
        r"(?:公司|企业)?(?:的)?(?:差旅|出差)(?:标准|制度)[。.!！?？\s]*", text))


class Supervisor:
    def __init__(self, model, services, store, config):
        self.model, self.services, self.store, self.config = model, services, store, config

    async def run(self, scope, text, *, user_text=None, progress=None, trip_input=None):
        run = Turn(self, scope, text, user_text if user_text is not None else text, progress)
        run.trip_input = trip_input
        return await run.execute()

    async def cancel(self, scope):
        return await self.store.call("cancel", scope)


class Turn:
    def __init__(self, runtime, scope, text, user_text, progress):
        self.runtime, self.scope, self.text, self.user_text = runtime, scope, text, user_text
        self.progress = progress
        self.state = {}
        self.owner = ""
        self.lock = asyncio.Lock()
        self.semaphore = asyncio.Semaphore(runtime.config["parallel_children"])
        self.task_locks = {}
        self.tool_locks = {}
        self.source_bytes = 0
        self.plan_lock = asyncio.Lock()
        self.trip_input = None

    async def publish_plan(self, *, outcome="running", reason=None):
        async with self.plan_lock:
            plan = execution_plan.snapshot(self.state, self.scope.request_id, outcome=outcome, reason=reason)
            await self.save()
            if self.progress:
                await self.progress({"type": "execution_plan", **plan})

    async def start_step(self, result_id):
        execution_plan.transition(self.state, result_id, "running")
        await self.publish_plan()

    async def save(self, response=None, status="running"):
        async with self.lock:
            snapshot = deepcopy(self.state)
            if len(json.dumps(snapshot, ensure_ascii=False, default=str)) > 1_000_000:
                raise ExecutionLimitExceeded("SUPERVISOR_CONTEXT_LIMIT", "本次任务上下文达到上限，请缩小任务范围")
            await self.runtime.store.call("save", self.scope, self.owner, snapshot, response, status)

    async def emit(self, phase, result_id=None, role=None):
        if self.progress:
            keys = {"analyzing": "request_analyzing", "queued": "queued", "running": "task_running",
                "completed": "task_completed", "failed": "task_failed", "done": "answer_ready"}
            await self.progress({"type": "task_status" if result_id else "status", "phase": phase,
                "message_key": keys.get(phase, "task_running"), "task_id": result_id,
                "intent": role, "display": PROFILES[role].title if role else None})

    async def _read_home_location(self):
        """Return the user's saved 常驻地 to pre-fill the departure field."""
        try:
            memory = getattr(self.runtime.services, "memory", None)
            if memory is None or not getattr(memory, "long_term", None):
                return ""
            preferences = await run_blocking(memory.long_term.get_preference)
            return str((preferences or {}).get("home_location") or "").strip()
        except Exception:
            return ""

    def intake(self, trip=None):
        saved = any(key in self.state.get("applied_results", []) and result["role"] == "trip_context"
                    and bool(result["data"].get("trip")) for key, result in self.state.get("results", {}).items())
        return intake_output(self.state["trip"] if trip is None else trip,
                             self.state.get("home_location", ""), saved=saved)

    def main_tool_names(self, state):
        names = set(MODELS)
        # Decide on an action before loading business manuals. This is a tool
        # boundary, not a request for the model to remember the prompt order.
        collecting = can_show_intake(self.state, self.deliverable_results()) and not evaluate_trip_intake(self.state["trip"])["planning_ready"]
        if not state.get("admitted") or collecting:
            names.discard("read_skill")
        if not any(key not in self.state.get("discarded_results", []) for key in self.state["results"]):
            names.difference_update({"read_result", "discard_result", "apply_changes"})
        from .validation import TRIP_FIELDS
        if not any(self.state["trip"].get(key) for key in TRIP_FIELDS):
            names.discard("prepare_trip_options")
        return names

    def deliverable_results(self):
        """Use the same evidence/write gates for both waiting and degraded exits."""
        selected = []
        for key in self.state["results"]:
            try:
                result = self.results([key])[0]
                if result.status not in {"success", "partial", "needs_input"}:
                    continue
                proposal = result.role == "trip_context" or (result.role == "memory" and result.data.get("preferences"))
                if proposal and key not in self.state["applied_results"]:
                    continue
                selected.append(result)
            except (ToolRejected, ValidationError):
                continue
        return selected

    def waiting_for_input(self):
        """Stop missing-input work while retaining independent usable results."""
        selected = self.deliverable_results()
        if can_show_intake(self.state, selected) and not evaluate_trip_intake(self.state["trip"])["planning_ready"]:
            return {**self.intake(), "outcome": "waiting_input"}
        question = "请说明你想处理的差旅事项，以及需要补充或修改的具体信息。"
        if selected:
            return {**render(Finish(kind="ask", question=question), selected), "outcome": "waiting_input"}
        return clarification_output(question)

    async def execute(self):
        record = await self.runtime.store.call("begin", self.scope, fingerprint({"text": self.text, "user_text": self.user_text}))
        self.owner = record["owner"]
        if record.get("response") is not None:
            return {**record["response"], "idempotent_replay": True}
        self.state = record.get("checkpoint") or {}
        try:
            if not self.state:
                context = await self.runtime.services.context(self.scope)
                previous = await self.runtime.store.call("previous", self.scope)
                pending = previous_question(previous, trip_version(context["trip"]))
                resolved = resolve_reply(self.user_text, pending) if self.text == self.user_text and not self.trip_input else None
                context["pending_input"], context["resolved_input"] = pending, resolved
                # Only a compact same-session work index. Raw historical sources
                # are deliberately not inherited; memory must retrieve them.
                if previous:
                    checkpoint = previous.get("checkpoint") or {}
                    context["previous_work"] = {"status": previous["status"], "results": [
                        {"role": r["role"], "summary": r["summary"][:500], "missing_info": r.get("missing_info", [])}
                        for r in list(checkpoint.get("results", {}).values())[-6:]]}
                    context["work_context"] = checkpoint.get("work_context", {})
                self.state = {"trip": context["trip"], "version": trip_version(context["trip"]), "results": {},
                    "checkpoint_version": 2, "work_items": {}, "control": {"status": "running"},
                    "work_context": context.get("work_context", {}),
                    "reply_to": pending, "resolved_input": resolved,
                    "children": {}, "calls": 0, "preferences_updated": False, "applied_results": [], "discarded_results": [],
                    "main": {"messages": [{"role": "system", "content": MAIN_RULES},
                        {"role": "user", "content": json.dumps({"context": context, "current_request": self.text}, ensure_ascii=False, default=str)}], "round": 0}}
                if previous and is_continue(self.user_text):
                    restored = restore_results(checkpoint, self.state["version"], self.source_fresh)
                    self.state["results"] = restored
                    self.state["applied_results"] = [i for i in checkpoint.get("applied_results", []) if i in restored]
                    self.state["work_items"] = {k: v for k, v in checkpoint.get("work_items", {}).items()
                        if v.get("result_id") in restored and v.get("status") in {"completed", "partial"}}
                    self.state["main"]["messages"].append({"role": "user", "content": json.dumps({
                        "completed_results": [SpecialistResult.model_validate(r).brief() for r in restored.values()],
                        "runtime_instruction": "这些是同会话、同版本且未过期的已完成结果，直接复用；只处理尚未完成的部分，不重复保存历史变更。"}, ensure_ascii=False)})
                await self.save()
            self.state.setdefault("checkpoint_version", 1)
            self.state.setdefault("work_items", {})
            self.state.setdefault("control", {"status": "running"})
            self.state["home_location"] = await self._read_home_location()
            execution_plan.resume(self.state)
            self.source_bytes = sum(encoded_size(c.get("sources", [])) for c in self.state["children"].values())
            await self.publish_plan()
            allow_short = bool(self.state.get("resolved_input") or self.trip_input or self.text != self.user_text
                               or is_intake_entry(self.user_text)
                               or (is_continue(self.user_text) and self.state.get("work_context")))
            guard = guard_user_input(self.user_text, allow_short_reply=allow_short)
            if guard and guard.intent == "unsupported":
                output = render(Finish(kind="refuse"), [])
                output["response"] = guard.clarification or output["response"]
            elif guard and guard.intent == "unclear":
                pending = self.state.get("reply_to")
                if pending and self.user_text.strip().isdecimal():
                    output = clarification_output(pending["question"], PendingInput.model_validate(pending["input"]))
                else:
                    output = clarification_output(guard.clarification)
            elif guard and guard.intent == "chitchat":
                output = {"response": "你好，我可以帮你查询企业差旅制度、整理行程、查询交通天气和个人差旅记录。", "answer_document": None, "presentation_document": None}
            elif self.text == self.user_text and is_intake_entry(self.user_text) and not evaluate_trip_intake(self.state["trip"])["planning_ready"]:
                output = self.intake()
                output["outcome"] = "waiting_input"
            else:
                await self.emit("analyzing")
                # DB cancellation works across workers, including during model waits.
                work = asyncio.create_task(self.execute_work())
                monitor = asyncio.create_task(self.monitor())
                try:
                    done, _ = await asyncio.wait({work, monitor}, timeout=self.runtime.config.get("turn_timeout_sec", 60), return_when=asyncio.FIRST_COMPLETED)
                    if not done:
                        raise asyncio.TimeoutError()
                    if monitor in done:
                        await monitor
                    output = await work
                except RuntimeStopped:
                    raise
                except ExecutionLimitExceeded as exc:
                    output = self.fallback(exc.code)
                except asyncio.TimeoutError:
                    output = self.fallback("TURN_TIMEOUT")
                except Exception as exc:
                    logger.warning("Supervisor degraded error_type=%s", type(exc).__name__)
                    output = self.fallback("UPSTREAM_UNAVAILABLE")
                finally:
                    work.cancel()
                    monitor.cancel()
                    await asyncio.gather(work, monitor, return_exceptions=True)
            output.update({"agents": [{"name": r["role"], "display": PROFILES[r["role"]].title, "status": r["status"], "duration_sec": 0}
                for r in self.state["results"].values()], "preferences_updated": self.state["preferences_updated"], "engine": "supervisor"})
            self.capture_evaluation()
            outcome = execution_plan.settle(self.state, output.get("outcome", "completed"), output.get("stop_reason"))
            output["outcome"] = outcome
            record_question(self.state, output)
            output["public_plan"] = execution_plan.snapshot(self.state, self.scope.request_id, outcome=outcome,
                reason="需要补充信息。" if outcome == "waiting_input" else "本轮处理已结束。")
            self.state["control"].update(status="completed", outcome=output.get("outcome", "completed"), stop_reason=output.get("stop_reason"))
            await self.save(output, "completed")
            if self.progress:
                await self.progress({"type": "execution_plan", **output["public_plan"]})
            await self.emit("done")
            return output
        except RuntimeStopped:
            execution_plan.settle(self.state, "cancelled")
            plan = execution_plan.snapshot(self.state, self.scope.request_id, outcome="cancelled", reason="本次执行已停止。")
            await self.runtime.store.call("stop", self.scope, self.owner, "interrupted", deepcopy(self.state))
            return {"response": "", "answer_document": None, "presentation_document": None, "agents": [],
                "preferences_updated": self.state.get("preferences_updated", False), "interrupted": True, "engine": "supervisor",
                "outcome": "cancelled", "public_plan": plan}
        except BaseException as exc:
            try:
                cancelled = isinstance(exc, asyncio.CancelledError)
                execution_plan.settle(self.state, "cancelled" if cancelled else "failed")
                execution_plan.snapshot(self.state, self.scope.request_id, outcome="cancelled" if cancelled else "failed", reason="本次执行已停止。")
                await self.runtime.store.call("stop", self.scope, self.owner, "interrupted" if cancelled else "failed", deepcopy(self.state))
            except Exception:
                logger.exception("Unable to record supervisor stop")
            raise

    @staticmethod
    def source_fresh(result):
        for source in result.sources:
            if source.get("kind") in {"train", "weather", "hotel", "commute", "trip_options"}:
                try:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(source["retrieved_at"])).total_seconds()
                except (KeyError, ValueError, TypeError):
                    return False
                if not -60 <= age <= 900:
                    return False
        return True

    async def execute_work(self):
        routes = weather_policy_tasks(self.user_text) if self.text == self.user_text else None
        if routes:
            return await self.direct_queries(routes)
        if self.text == self.user_text and is_direct_policy_query(self.user_text):
            return await self.direct_policy()
        submitted = parse_intake_submission(self.user_text) if self.text == self.user_text else None
        resolved = self.state.get("resolved_input") or {}
        if resolved.get("field") == "duration_days" and type(resolved.get("value")) is int:
            submitted = {"trip": {"duration_days": resolved["value"]},
                         "field_sources": {"duration_days": self.user_text}}
        if self.trip_input:
            fields = {k: v for k, v in self.trip_input.items() if k in {"origin", "destination", "start_date", "end_date", "duration_days", "trip_purpose", "work_location", "work_schedule"} and v not in (None, "")}
            submitted = {"trip": fields, "field_sources": {k: self.user_text for k in fields}}
        if not submitted and self.text == self.user_text and not evaluate_trip_intake(self.state["trip"])["planning_ready"]:
            submitted = parse_trip_entry(self.user_text)
        if submitted and not self.state.get("intake_submission_applied"):
            result_id = "result_intake_submission"
            execution_plan.register(self.state, "intake_submission", WorkItem(role="trip_context", task="保存用户提交的行程字段",
                input_version=self.state["version"], result_id=result_id).model_dump())
            await self.publish_plan()
            await self.start_step(result_id)
            report_call = {"name": "report", "arguments": {"summary": "已接收行程字段。", "data": submitted}}
            accepted = await self.invoke_specialist(report_call, "trip_context", SourceScope(), [])
            result = SpecialistResult(**accepted["_terminal"], result_id=result_id, role="trip_context", task="保存用户提交的行程字段", input_version=self.state["version"])
            self.state["results"][result_id] = result.model_dump()
            await self.invoke_main(self.state["main"], {"name": "apply_changes", "arguments": {"result_id": result_id}})
            trip = self.state["trip"]
            summary = "行程已保存：" + "；".join(f"{label}：{trip[key]}" for key, label in (
                ("origin", "出发地"), ("destination", "目的地"), ("start_date", "出发日期"),
                ("duration_days", "出差天数"), ("end_date", "返程日期"), ("trip_purpose", "出差目的")) if trip.get(key))
            self.state["results"][result_id]["summary"] = summary
            self.state["intake_submission_applied"] = True
            self.state["work_items"]["intake_submission"]["input_version"] = self.state["version"]
            execution_plan.complete(self.state, result_id)
            self.state["main"]["messages"].append({"role": "user", "content": json.dumps({
                "runtime_committed_trip": trip, "completed_results": [self.state["results"][result_id]],
                "runtime_instruction": "这些字段已由运行时验证并保存，直接使用该结果，不再提取或保存同一行程。"}, ensure_ascii=False)})
            await self.save()
            await self.publish_plan()
            await self.emit("completed", result_id, "trip_context")
        if submitted and not evaluate_trip_intake(self.state["trip"])["planning_ready"]:
            return {**self.intake(), "outcome": "waiting_input"}
        if submitted and hasattr(self.runtime.services, "prepare_trip_options") and not any(word in self.user_text for word in ("合规", "检查", "差旅标准", "差旅制度")):
            return await self.prepare_options()
        return await self.loop(self.state["main"], None, None, [])

    async def prepare_options(self):
        from .validation import TRIP_FIELDS
        if not any(self.state["trip"].get(key) for key in TRIP_FIELDS):
            raise ToolRejected("没有可用于规划的行程信息", code="EMPTY_TRIP_CONTEXT", next_action="ask_user")
        if any(r["role"] == "trip_context" and (r["data"].get("trip") or r["data"].get("trip_action", "update") != "update")
               and key not in self.state["applied_results"] and key not in self.state["discarded_results"]
               for key, r in self.state["results"].items()):
            raise ToolRejected("请先保存本轮行程，再查询对应版本的车次和酒店", code="UNCOMMITTED_TRIP", next_action="apply_changes")
        if not evaluate_trip_intake(self.state["trip"])["planning_ready"]:
            return {**self.intake(), "outcome": "waiting_input"}
        from .trip_options import options_output, exclusions
        from core.integrations.places.service import validated_trip_anchor
        selection = (self.trip_input or {}).get("capability_selection") or self.state["trip"].get("_capability_selection") or {}
        selection = {"include": list(selection.get("include", [])), "exclude": sorted(exclusions(self.user_text, selection))}
        if "nearby_hotels" not in selection["exclude"] and not validated_trip_anchor(self.state["trip"]):
            return {**self.intake({**self.state["trip"], "_capability_selection": selection}), "outcome": "waiting_input"}
        step_id = "trip_choice_queries"
        execution_plan.register(self.state, step_id, WorkItem(role="travel_info", task="查询车次和工作地点附近酒店",
            input_version=self.state["version"], result_id=step_id).model_dump(), title="查询车次与附近酒店", purpose="获取真实候选并根据偏好排序")
        await self.start_step(step_id)
        for key in ("weather", "local_transport"):
            if key not in selection["exclude"] and key not in selection["include"]:
                selection["include"].append(key)
        board = await self.runtime.services.prepare_trip_options(self.state["trip"], selection=selection, user_text=self.user_text)
        output = options_output(board)
        status = "needs_input" if output["outcome"] == "waiting_input" else "partial" if output["outcome"] == "partial" else "success"
        sources = SourceScope()
        source_id = sources.add("trip_options", board.model_dump(mode="json"))["source_id"]
        sources.read(source_id)
        summary = f"已返回 {len(board.trains)} 个车次、{len(board.hotels)} 家附近酒店。"
        from .full_trip import planning_facts
        report = Report(status=status, summary=summary, evidence_refs=[source_id], data={"choices": planning_facts(board), "findings": [
            {"item": "出行候选", "conclusion": summary, "evidence_refs": [source_id]}]})
        validate_report("travel_info", report, sources, [])
        self.state["results"][step_id] = SpecialistResult(**report.model_dump(), role="travel_info", result_id=step_id,
            task="查询车次和附近酒店", input_version=self.state["version"], sources=list(sources.sources.values())).model_dump()
        execution_plan.transition(self.state, step_id, "needs_input" if output["outcome"] == "waiting_input" else "partial" if output["outcome"] == "partial" else "completed")
        self.state["choice_output"] = {"version": self.state["version"], "output": deepcopy(output)}
        await self.publish_plan()
        # This entry point serves a complete business trip, including form
        # submissions. Candidate retrieval alone must never finish the turn.
        return await self.complete_trip(output, step_id)

    async def complete_trip(self, output, choices_id):
        from .full_trip import complete_output
        parent = self.state["main"]
        guidance = await self.invoke(parent, {"name": "read_skill", "arguments": {
            "name": "plan-trip", "resource": "references/complete-trip.md"}}, None, None, [])
        selected = []
        tasks = [
            ("policy_rag", "核实本次完整企业差旅的目的地城市等级、各职级住宿限额、餐补、铁路和航空席别条件、市内交通及接驳、住宿核算和报销凭证要求。不同费用保留适用条件；职级未知列已核实档位，不默认普通员工。未知项如实列出，不能仅查车票与酒店。"),
            ("trip_planner", "根据本次已保存行程、真实车次酒店、偏好和制度形成每日差旅安排。覆盖去程、工作、住宿与返程待确认项；会议时间未知不得编造时间或保证候选能准时到达。候选未被用户选定就保持候选措辞。不得编造返程车次、房价或整体合规。"),
        ]
        if any(word in self.user_text for word in ("合规", "检查")):
            tasks.append(("compliance", "依据本轮方案和制度检查差旅资格及报销条件。未选定车次酒店、缺职级或真实房价时，明确 unknown/partial，不把候选方案判定整体合规。"))
        for role, task in tasks:
            existing = []
            for key, raw in self.state["results"].items():
                if raw["role"] != role:
                    continue
                try:
                    existing.extend(self.results([key]))
                except ToolRejected:
                    continue
            # Reuse verified policy, but a planner must see this exact board.
            board_refs = set(self.state["results"][choices_id]["evidence_refs"])
            existing = [r for r in existing if role == "policy_rag" or board_refs <= {s["id"] for s in r.sources}]
            if existing:
                result = existing[-1]
            else:
                ids = [choices_id, *[r.result_id for r in selected if r.status in {"success", "partial"}]] if role in {"trip_planner", "compliance"} else []
                if role in {"trip_planner", "compliance"} and any(r.status not in {"success", "partial"} for r in selected):
                    task += "本次制度查询未完成；仅提供有依据的安排，差标和报销资格标待核实，报告 partial。"
                brief = await self.delegate(parent, {"id": "complete_trip_" + role, "name": "delegate",
                    "skill_guidance": guidance["guidance"] if role == "trip_planner" else ""},
                    Delegate(role=role, task=task, result_ids=ids))
                result = self.results([brief["result_id"]])[0]
            selected.append(result)
        output = complete_output(output, selected)
        await self.publish_plan(outcome=output["outcome"])
        return output

    def fallback(self, reason):
        selected = self.deliverable_results()
        if can_show_intake(self.state, selected) and not evaluate_trip_intake(self.state["trip"])["planning_ready"]:
            output = self.intake()
            output.update(outcome="degraded", stop_reason=reason)
            return output
        choices = self.state.get("choice_output")
        if choices and choices["version"] == self.state["version"]:
            from .full_trip import complete_output
            output = complete_output(choices["output"], [r for r in selected if r.role not in {"trip_context", "travel_info"}])
            return {**output, "outcome": "degraded", "stop_reason": reason}
        return degraded_output(selected, reason)

    def completed_form_plan(self):
        # The intake card's concrete deliverable is a trip plan. Once this
        # artifact exists, do not spend extra model rounds reading/rephrasing
        # it or waiting for the model to remember finish.
        if not self.state.get("intake_submission_applied") or any(word in self.user_text for word in ("合规", "检查")):
            return None
        selected = []
        for key in self.state["results"]:
            try:
                result = self.results([key])[0]
                if result.status not in {"success", "partial"}:
                    continue
                if (result.role == "trip_context" or (result.role == "memory" and result.data.get("preferences"))) and key not in self.state["applied_results"]:
                    continue
                selected.append(result)
            except ToolRejected:
                continue
        if any(r.role == "trip_planner" for r in selected):
            output = render(Finish(result_ids=[r.result_id for r in selected]), selected)
            output["outcome"] = "partial" if any(r.status == "partial" for r in selected) else "completed"
            return output
        return None

    async def direct_policy(self):
        # Clear standard lookups need one bounded specialist, no supervisor
        # planning/read-result/redelegation loop. Persistence and cancellation
        # use exactly the same turn, child and provenance paths as other tasks.
        call = {"id": "direct_policy", "name": "delegate"}
        brief = await self.delegate(self.state["main"], call, Delegate(role="policy_rag", task=self.user_text))
        selected = self.results([brief["result_id"]])
        if selected[0].status in {"unavailable", "error"}:
            return self.fallback("NO_PROGRESS")
        return render(Finish(result_ids=[brief["result_id"]]), selected)

    async def direct_queries(self, routes):
        # Register every requested deliverable before starting either leaf.
        for route in routes:
            request = Delegate(role=route["role"], task=route["task"])
            execution_plan.register(self.state, task_key(request, self.state["version"]),
                WorkItem(role=request.role, task=request.task, input_version=self.state["version"],
                         result_id=route["step_id"]).model_dump(), title=route["title"], purpose=route["purpose"])
        await self.publish_plan(reason="天气与制度可分别查询，任一项失败时保留另一项结果。")
        async def query(route):
            try:
                return await self.delegate(self.state["main"], {"id": route["step_id"], "step_id": route["step_id"], "request_text": route["task"]},
                    Delegate(role=route["role"], task=route["task"]))
            except ToolRejected:
                execution_plan.transition(self.state, route["step_id"], "failed")
                await self.publish_plan()
                return None
        tasks = [asyncio.create_task(query(route)) for route in routes]
        try:
            briefs = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        selected = self.results([b["result_id"] for b in briefs if b])
        if len(selected) != len(routes) or any(r.status in {"unavailable", "error"} for r in selected):
            return self.fallback("NO_PROGRESS")
        return render(Finish(result_ids=[r.result_id for r in selected]), selected)

    def capture_evaluation(self):
        # Post-processing must never fail the business turn.
        from evaluation.collector import current_collector
        collector = current_collector()
        if collector is None:
            return
        try:
            initial = json.loads(self.state["main"]["messages"][1]["content"])
            collector.record_context(initial.get("context", {}).get("recent", []))
            collector.record_runtime(self.scope.request_id, list(self.state["results"].values()))
        except Exception:
            logger.exception("Unable to capture supervisor evaluation metadata")

    async def monitor(self):
        while True:
            await self.runtime.store.call("check", self.scope, self.owner)
            await asyncio.sleep(0.5)

    async def loop(self, state, role, sources, dependencies):
        if "terminal" in state:
            return state["terminal"]
        max_rounds = self.runtime.config["main_rounds" if role is None else "child_rounds"]
        if role == "policy_rag":
            max_rounds = min(max_rounds, 5)  # fourth round reports; fifth only repairs output
        elif role in {"memory", "travel_info"}:
            max_rounds = min(max_rounds, 4)
        report_round = min(max_rounds, 4) if role == "policy_rag" else min(max_rounds, 3) if role in {"memory", "travel_info"} else max(1, max_rounds - 1)
        tools = MAIN_TOOLS if role is None else tool_schemas(PROFILES[role].tools) + [
            schema("read_skill", "读取本角色允许的业务 Skill", ReadSkill), schema("report", "提交有据摘要及结构化数据", REPORT_MODELS[role])]
        if role is not None:
            next(t for t in tools if t["function"]["name"] == "read_skill")["function"]["parameters"]["properties"]["name"]["enum"] = list(PROFILES[role].skills)
        if role in {"trip_context", "trip_planner", "compliance"}:
            # These roles transform supplied facts; their business rules are
            # already in the system profile. No exploratory tool loop needed.
            tools = [t for t in tools if t["function"]["name"] == "report"]
            max_rounds = min(max_rounds, 2)
            report_round = 1
        while state["round"] < max_rounds or state.get("pending"):
            await self.runtime.store.call("check", self.scope, self.owner)
            if not state.get("pending") and state.get("no_progress", 0) >= 3:
                if role is None:
                    return self.fallback("NO_PROGRESS")
                state["force_report"] = True
            if not state.get("pending") and state.get("report_errors", 0) >= 2:
                break
            if not state.get("pending"):
                saved_chars = compact_tool_history(state["messages"])
                if saved_chars:
                    logger.info("Supervisor context compacted role=%s saved_chars=%s", role or "main", saved_chars)
                if encoded_size(state["messages"]) + encoded_size(tools) > 80000:
                    raise ExecutionLimitExceeded("SUPERVISOR_CONTEXT_LIMIT", "本次任务资料过多，请缩小问题范围后继续")
                state["round"] += 1
                # Reserve the last child round for extracting a bounded report,
                # rather than another search which cannot be consumed in time.
                report_only = role is not None and (state["round"] >= report_round or state.get("force_report"))
                round_tools = [t for t in tools if t["function"]["name"] == "report"] if report_only else tools
                if role is None:
                    round_tools = [t for t in tools if t["function"]["name"] in self.main_tool_names(state)]
                if role == "policy_rag" and not report_only:
                    # Tool availability enforces retrieve -> read -> extract.
                    # A prompt alone allowed repeated search followed by claims
                    # from discovery snippets without ever reading evidence.
                    allowed = {"search_policy"} if state["round"] == 1 else {"read_source", "report"}
                    round_tools = [t for t in tools if t["function"]["name"] in allowed]
                    if state["round"] == 2:
                        state["messages"].append({"role": "user", "content": "检索阶段已结束。现在同轮 read_source 回读相关来源，随后提取报告。不得把检索摘要当作完整证据；缺少的类别如实标为未知。"})
                elif role in {"memory", "travel_info"} and not report_only:
                    allowed = (set(PROFILES[role].tools) - {"read_source"}) | {"report"} if state["round"] == 1 else {"read_source", "report"}
                    round_tools = [t for t in tools if t["function"]["name"] in allowed]
                if report_only:
                    instruction = "本角色直接整理提供的输入，调用 report 返回结构化结果；不需要查询，也不代表其他角色的查询预算已用完。" if role in {"trip_context", "trip_planner", "compliance"} else "查询预算已用完。现在调用 report，压缩已读资料为结论、适用条件、未知项和 evidence_refs；未核实事项标为 partial/unavailable，不再查询。"
                    state["messages"].append({"role": "user", "content": instruction})
                try:
                    logger.info("Supervisor model input role=%s round=%s message_chars=%s tool_chars=%s", role or "main", state["round"], encoded_size(state["messages"]), encoded_size(round_tools))
                    reply = await call_model(self.runtime.model, state["messages"], round_tools)
                except ToolRejected as exc:
                    logger.warning("Native tool response rejected role=%s reason=%s", role or "main", str(exc))
                    state["messages"].append({"role": "user", "content": str(exc) + "；请输出完整有效的原生工具调用。"})
                    state["no_progress"] = state.get("no_progress", 0) + 1
                    await self.save()
                    continue
                if not reply.calls:
                    state["messages"].append({"role": "user", "content": "请使用提供的原生工具，完成后调用 finish 或 report。"})
                    state["no_progress"] = state.get("no_progress", 0) + 1
                    await self.save()
                    continue
                state["pending"] = reply.calls
                state["outputs"] = {}
                state["messages"].append(assistant_message(reply.calls))
                await self.save()  # Before any tool execution: replay knows pending calls.
            calls = state["pending"]
            # Reject stale/out-of-phase calls even when a provider ignores the
            # current tool schema; pending calls retain their original phase.
            phase_names = {t["function"]["name"] for t in tools}
            if role is None:
                phase_names = self.main_tool_names(state)
            elif role == "policy_rag":
                phase_names = {"report"} if state["round"] >= report_round or state.get("force_report") else {"search_policy"} if state["round"] == 1 else {"read_source", "report"}
            elif role in {"memory", "travel_info"}:
                phase_names = {"report"} if state["round"] >= report_round or state.get("force_report") else (set(PROFILES[role].tools) - {"read_source"}) | {"report"} if state["round"] == 1 else {"read_source", "report"}
            elif role is not None and (state["round"] >= report_round or state.get("force_report")):
                phase_names = {"report"}
            # Delegate independent leaves and execute read-only lookups together.
            # Mutations and finish stay exclusive to preserve replay semantics.
            exclusive = any(c["name"] in {"finish", "report", "apply_changes", "prepare_trip_options"} for c in calls)
            if len(calls) > 6:
                values = [failure("BATCH_LIMIT", "同轮最多6个工具调用；一次 report 汇总全部 findings。") for _ in calls]
            elif any(c["name"] not in phase_names for c in calls):
                action = "report" if role else "finish" if self.state["results"] else "ask_user"
                values = [failure("PHASE_VIOLATION", "当前阶段只允许：" + "、".join(sorted(phase_names)), action) for _ in calls]
            elif exclusive and len(calls) != 1:
                values = [failure("EXCLUSIVE_TOOL", "finish、report、apply_changes 必须单独一轮调用") for _ in calls]
            elif (role is None and all(c["name"] == "delegate" for c in calls)) or (role is not None and all(c["name"] in PROFILES[role].tools for c in calls)):
                tasks = [asyncio.create_task(self.invoke_cached(state, c, role, sources, dependencies)) for c in calls]
                try:
                    values = await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            else:
                values = [await self.invoke_cached(state, c, role, sources, dependencies) for c in calls]
            for call, value in zip(calls, values):
                state["messages"].append(tool_message(call, value))
            if role is None and any(c["name"] == "delegate" and v.get("result_id") for c, v in zip(calls, values)):
                state["admitted"] = True
            progressed = any(made_progress(c, v) for c, v in zip(calls, values))
            preparing = any(c["name"] == "read_skill" and v.get("guidance") and not v.get("error")
                            and not v.get("reused") for c, v in zip(calls, values))
            if progressed:
                state["no_progress"] = 0
            elif preparing and state.get("preparation_rounds", 0) < 3:
                # A guide plus its two references is legitimate preparation.
                # A bounded grace period never resets existing failure debt.
                state["preparation_rounds"] = state.get("preparation_rounds", 0) + 1
            else:
                state["no_progress"] = state.get("no_progress", 0) + 1
            state["report_errors"] = state.get("report_errors", 0) + sum(c["name"] == "report" and bool(v.get("error")) for c, v in zip(calls, values))
            state.pop("pending", None)
            state.pop("outputs", None)
            # Terminal value persists with its tool reply, so a retry does not
            # rerun a completed report/finish after a lost HTTP connection.
            if len(values) == 1 and isinstance(values[0], dict) and "_terminal" in values[0]:
                state["terminal"] = values[0]["_terminal"]
            if role is None and "terminal" not in state:
                actions = {(v.get("failure") or {}).get("next_action") for v in values}
                empty_intake = any(v.get("role") == "trip_context" and v.get("status") == "needs_input"
                                   and not v.get("committed") for v in values)
                if "ask_user" in actions or empty_intake:
                    state["terminal"] = self.waiting_for_input()
                elif "finish" in actions:
                    state["terminal"] = self.fallback("NO_PROGRESS")
            if role is None and "terminal" not in state:
                # Once this turn's extraction has been validated, a normal trip
                # request has a concrete next step. Do not ask the model to
                # repeatedly rediscover the query/choice workflow.
                collected = any(v.get("role") == "trip_context" and v.get("status") in {"success", "partial"}
                                and v.get("data", {}).get("trip_action") != "cancel" for v in values)
                simple_planning = (any(word in self.user_text for word in ("出差", "规划", "安排行程"))
                    and not any(word in self.user_text for word in ("制度", "标准", "合规", "报销", "历史", "记忆", "记住", "取消")))
                if collected and simple_planning and evaluate_trip_intake(self.state["trip"])["planning_ready"] and hasattr(self.runtime.services, "prepare_trip_options"):
                    state["terminal"] = await self.prepare_options()
            if role is None and "terminal" not in state:
                delivered = self.completed_form_plan()
                if delivered is not None:
                    state["terminal"] = delivered
            await self.save()
            if "terminal" in state:
                return state["terminal"]
        raise ExecutionLimitExceeded("SUPERVISOR_ROUND_LIMIT", "本次任务达到处理轮数上限，请缩小范围后继续")

    async def invoke_cached(self, state, call, role, sources, dependencies):
        normalized = call["arguments"]
        if role is not None and call["name"] in TOOLS:
            try:
                normalized = TOOLS[call["name"]][0].model_validate(normalized).model_dump()
                if call["name"] == "search_memory" and normalized["kind"] == "preferences":
                    # The repository reads the same preference row regardless
                    # of the search string/limit. Do not pay for synonyms.
                    normalized = {"kind": "preferences"}
            except ValidationError:
                pass  # invoke returns the structured field error below
        operation = fingerprint({"name": call["name"], "arguments": normalized})
        lock = self.tool_locks.setdefault((id(state), operation), asyncio.Lock())
        async with lock:
            return await self._invoke_cached(state, call, role, sources, dependencies, operation)

    async def _invoke_cached(self, state, call, role, sources, dependencies, operation):
        if call["id"] in state["outputs"]:
            return state["outputs"][call["id"]]
        try:
            # A repeated read or identical external query must not expand the
            # context or reissue network traffic. Checkpoints retain this ledger.
            repeatable = role is not None and call["name"] in PROFILES[role].tools
            if repeatable and operation in state.get("operations", {}):
                raise ToolRejected("该查询或来源页已经处理；使用已读证据汇报，不重复查询。",
                    code="DUPLICATE_CALL", next_action="report")
            value = await self.invoke(state, call, role, sources, dependencies)
            if repeatable:
                state.setdefault("operations", {})[operation] = {"tool": call["name"], "status": "failed" if value.get("error") else "completed"}
        except (ToolRejected, ValidationError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                violations = [{"field": ".".join(map(str, e["loc"])), "rule": e["type"],
                    "constraints": ", ".join(f"{k}={v}" for k, v in (e.get("ctx") or {}).items() if k in {"max_length", "min_length", "le", "ge", "expected"})}
                    for e in exc.errors(include_input=False, include_url=False)][:12]
                fields = [v["field"] for v in violations]
                message = "请修正字段：" + "；".join(f'{v["field"]}: {v["rule"]} {v["constraints"]}' for v in violations)
                value = failure("SCHEMA_VALIDATION", message[:500], fields=fields, violations=violations)
            elif isinstance(exc, ToolRejected):
                value = exc.failure.wire()
            else:
                value = failure("INVALID_ARGUMENT", "参数值无效，请检查日期、字段及类型")
            state["last_error"] = value["failure"]
        state["outputs"][call["id"]] = value
        if sources is not None:
            state["sources"] = list(sources.sources.values())
            state["read_ids"] = list(sources.read_ids)
        await self.save()
        return value

    def results(self, ids):
        if len(ids) != len(set(ids)) or any(i not in self.state["results"] or i in self.state["discarded_results"] for i in ids):
            raise ToolRejected("结果 ID 不存在或重复")
        values = [SpecialistResult.model_validate(self.state["results"][i]) for i in ids]
        if any(r.input_version != self.state["version"] for r in values):
            raise ToolRejected("结果对应旧版行程，请重新委派受影响任务")
        for result in values:
            for source in result.sources:
                if source.get("kind") in {"train", "weather", "hotel", "commute", "trip_options"}:
                    try:
                        age = (datetime.now(timezone.utc) - datetime.fromisoformat(source["retrieved_at"])).total_seconds()
                    except (KeyError, ValueError, TypeError):
                        raise ToolRejected("出行资料缺少有效查询时间，请重新查询")
                    if age > 900 or age < -60:
                        raise ToolRejected("出行资料已过期，请重新查询后再规划")
        return values

    async def invoke(self, state, call, role, sources, dependencies):
        name, raw = call["name"], call["arguments"]
        if name == "read_skill":
            request = ReadSkill.model_validate(raw)
            allowed = {s for p in PROFILES.values() for s in p.skills} if role is None else set(PROFILES[role].skills)
            if request.name not in allowed:
                raise ToolRejected("该 Skill 不在本角色权限范围内")
            from utils.skill_loader import SkillLoader
            loader = SkillLoader()
            if request.resource:
                if not request.resource.startswith("references/") or ".." in request.resource or "\\" in request.resource:
                    raise ToolRejected("仅可读取该 Skill 的 references/ 参考资料")
                guidance = await run_blocking(loader.get_skill_resource, request.name, request.resource)
            else:
                guidance = await run_blocking(loader.get_skill_content, request.name)
            if not guidance:
                raise ToolRejected("该业务指南当前不可用")
            key = request.name + ":" + request.resource
            reads = state.setdefault("skills_read", [])
            reused = key in reads
            if not reused:
                reads.append(key)
            return {"skill": request.name, "resource": request.resource, "guidance": guidance[:12000], "reused": reused}
        if role is not None:
            return await self.invoke_specialist(call, role, sources, dependencies)
        return await self.invoke_main(state, call)

    async def invoke_specialist(self, call, role, sources, dependencies):
        """Leaves can query and report; they cannot delegate or commit changes."""
        name, raw = call["name"], call["arguments"]
        if name == "report":
            request = Report.model_validate(REPORT_MODELS[role].model_validate(raw).model_dump(exclude_none=True))
            if role == "trip_context":
                # On an empty trip, create and update are the same operation.
                # The explicit-new guard exists to prevent discarding an active
                # trip, not to demand magic words for the first form submission.
                if not self.state["trip"] and request.data.get("trip_action") == "new":
                    request.data["trip_action"] = "update"
                if (self.state["trip"] and request.data.get("trip_action") == "new" and not request.data.get("action_source")
                    and request.data.get("trip") and all(self.state["trip"].get(k) == v for k, v in request.data["trip"].items())
                    and not any(word in self.user_text for word in ("新行程", "新的出差", "新出差", "另一次出差", "重新开始"))):
                    # Repeating identical values cannot replace the active
                    # trip. Treat this as a read/no-op, never as a new write.
                    request.data["trip_action"] = "update"
                omitted = set(request.data.get("field_sources", {})) - set(request.data.get("trip", {}))
                # A leaf may cite unchanged committed values without proposing
                # them again. Drop only citations that demonstrably describe
                # the existing value; genuinely new omitted fields still fail.
                if request.data.get("trip_action", "update") == "update":
                    from .validation import grounded_date
                    for key in list(omitted):
                        old = self.state["trip"].get(key)
                        quote = request.data["field_sources"][key]
                        same = bool(old is not None and str(old) in quote)
                        if old and key in {"start_date", "end_date"}:
                            same = grounded_date(str(old), quote)
                        if old and key == "duration_days":
                            same = bool(re.search(rf"(?<!\d){old}\s*天|天数[：:]\s*{old}(?!\d)", quote))
                        if same:
                            omitted.remove(key)
                            request.data["field_sources"].pop(key)
                if omitted:
                    raise ToolRejected("字段已有原文依据，但 data.trip 遗漏对应值；请补齐，不要只写进 summary。",
                                       code="MISSING_TRIP_FIELDS", fields=["data.trip." + k for k in sorted(omitted)])
                proposal = bool(request.data.get("trip") or request.data.get("trip_action", "update") != "update")
                if proposal:
                    candidate = SpecialistResult(**request.model_dump(), result_id="validation", role=role, task="validate")
                    validated_changes(candidate, self.user_text, self.state["trip"], {}, resolved_input=self.state.get("resolved_input"))
                elif request.status == "success" and not self.state["trip"]:
                    raise ToolRejected("不能把空行程标记成功；填写 data.trip 和 field_sources，无法提取时返回 needs_input 并列出缺项。",
                                       code="EMPTY_TRIP_REPORT", fields=["data.trip", "data.field_sources"])
                if request.status in {"success", "partial", "needs_input"} and request.data.get("trip_action") != "cancel":
                    base = {} if request.data.get("trip_action") == "new" else self.state["trip"]
                    intake = evaluate_trip_intake({**base, **request.data.get("trip", {})})
                    if not intake["planning_ready"]:
                        request.status = "needs_input"
                        request.missing_info = intake["missing_required"]
            if role == "memory" and request.data.get("preferences"):
                preferences = await run_blocking(self.runtime.services.memory.long_term.get_preference)
                # Repeating retrieved preferences is a read, not a write
                # proposal. Only actual differences pass the original-text gate.
                request.data["preferences"] = {k: v for k, v in request.data["preferences"].items() if preferences.get(k) != v}
                if request.data["preferences"]:
                    candidate = SpecialistResult(**request.model_dump(), result_id="validation", role=role, task="validate")
                    validated_changes(candidate, self.user_text, self.state["trip"], preferences)
            if role in {"policy_rag", "memory", "travel_info"} and request.data.get("findings"):
                findings = request.data["findings"]
                known = set(sources.sources)
                if any(set(item["evidence_refs"]) - known for item in findings):
                    raise ToolRejected("报告引用了本任务不存在的来源", code="UNKNOWN_EVIDENCE", fields=["data.findings.evidence_refs"])
                verified = lambda item: bool(item["evidence_refs"]) and set(item["evidence_refs"]) <= sources.read_ids
                unsupported = [item["item"] for item in findings if not verified(item)]
                request.data["findings"] = [item for item in findings if verified(item)]
                request.evidence_refs = list(dict.fromkeys(ref for item in request.data["findings"] for ref in item["evidence_refs"]))
                if unsupported:
                    request.status = "partial"
                    request.summary = "已核实的标准如下，其余事项仍需确认。" if role == "policy_rag" else "已核实的信息如下，其余事项仍需确认。"
                    request.missing_info = list(dict.fromkeys([*request.missing_info, *unsupported]))[:12]
            data_limit = 7000 if role == "policy_rag" else 4000 if role == "memory" else 12000
            if len(json.dumps(request.data, ensure_ascii=False)) > data_limit:
                raise ToolRejected("结果过大，请压缩结构化数据")
            validate_report(role, request, sources, dependencies)
            if role in {"policy_rag", "memory", "travel_info"} and not request.evidence_refs and not (role == "memory" and request.data.get("preferences")):
                request = Report(status="needs_input" if request.missing_info else "unavailable",
                    summary="本次未取得可核实的资料，无法给出确定结论。", missing_info=request.missing_info)
            return {"_terminal": request.model_dump()}
        if name not in PROFILES[role].tools:
            raise ToolRejected("本角色没有该工具权限")
        request = TOOLS[name][0].model_validate(raw)
        if name == "read_source":
            return sources.read(request.source_id, request.offset, request.limit)
        try:
            kind, data = await asyncio.wait_for(self.runtime.services.execute(self.scope, name, request), timeout=self.runtime.config["tool_timeout_sec"])
        except (ExecutionLimitExceeded, RuntimeStopped, ToolRejected):
            raise
        except Exception as exc:
            logger.warning("Specialist tool unavailable role=%s tool=%s error_type=%s", role, name, type(exc).__name__)
            return failure("SOURCE_TIMEOUT" if isinstance(exc, asyncio.TimeoutError) else "SOURCE_UNAVAILABLE",
                           "该数据源本次查询不可用；交付其他已核实结果并明确缺失部分。", "report")
        entries = data if kind in {"policy", "memory"} and isinstance(data, list) else [data]
        index, evidence_budget = [], 7000
        for item in entries[:20]:
            # Reject oversize observations before inserting them into durable
            # state. Otherwise even the fallback's final checkpoint can exceed
            # the same limit and fail a second time.
            size = encoded_size(item)
            if size > 50000 or self.source_bytes + size > 200000:
                if not index:
                    return failure("SOURCE_SIZE_LIMIT", "资料超过本轮接收上限；请交付已有证据并明确缺失项。", "report")
                break
            self.source_bytes += size
            entry = sources.add(kind, item)
            if kind == "policy" and not entry["already_read"]:
                view = sources._view(sources.sources[entry["source_id"]])
                size = encoded_size(view)
                if size <= min(2500, evidence_budget):
                    # Full, bounded evidence is actually delivered to this leaf,
                    # eliminating a model turn whose only purpose was to fetch
                    # the same short passage again. Long sources remain paged.
                    entry["evidence"] = sources.read(entry["source_id"])
                    entry["already_read"] = True
                    evidence_budget -= size
            index.append(entry)
        return {"sources": index, "empty": not entries}

    async def invoke_main(self, state, call):
        """The supervisor owns delegation, review, commits and final selection."""
        name, raw = call["name"], call["arguments"]
        if name not in MODELS:
            raise ToolRejected("主 Agent 只能委派、审阅和提交已验证结果")
        request = MODELS[name].model_validate(raw)
        if name == "prepare_trip_options":
            return {"_terminal": await self.prepare_options()}
        if name == "delegate":
            return await self.delegate(state, call, request)
        if name == "read_result":
            return self.results([request.result_id])[0].model_dump(exclude={"sources"})
        if name == "discard_result":
            if request.result_id not in self.state["results"]:
                raise ToolRejected("结果不存在")
            if request.result_id not in self.state["discarded_results"]:
                self.state["discarded_results"].append(request.result_id)
            return {"discarded": request.result_id, "committed_writes_unchanged": request.result_id in self.state["applied_results"]}
        if name == "apply_changes":
            result = self.results([request.result_id])[0]
            if request.result_id in self.state["applied_results"]:
                return {"applied": True, "reused": True, "trip": self.state["trip"], "version": self.state["version"]}
            preferences = await run_blocking(self.runtime.services.memory.long_term.get_preference)
            trip, prefs, action = validated_changes(result, self.user_text, self.state["trip"], preferences,
                                                   resolved_input=self.state.get("resolved_input"))
            if result.role == "trip_context":
                if self.trip_input:
                    if "capability_selection" in self.trip_input:
                        trip["_capability_selection"] = self.trip_input["capability_selection"]
                    if not self.trip_input.get("work_location"):
                        trip["work_location"] = None
                        trip["work_location_verified"] = None
                # A changed city/location invalidates the old provider identity.
                if any(k in trip and trip[k] != self.state["trip"].get(k) for k in ("destination", "work_location")):
                    trip["work_location_verified"] = None
                    if "destination" in trip and trip["destination"] != self.state["trip"].get("destination") and "work_location" not in trip:
                        trip["work_location"] = None
                verified = (self.trip_input or {}).get("work_location_verified")
                if verified:
                    from core.integrations.places.models import VerifiedPlace
                    from core.integrations.places.service import place_matches_city
                    anchor = VerifiedPlace.model_validate(verified)
                    if not place_matches_city(anchor, trip.get("destination", self.state["trip"].get("destination", ""))) or anchor.name != trip.get("work_location", self.state["trip"].get("work_location")):
                        raise ToolRejected("所选工作地点与本次目的地不一致")
                    trip["work_location_verified"] = anchor.model_dump(mode="json")
            if action == "update" and all(self.state["trip"].get(k) == v for k, v in trip.items()) and all(preferences.get(k) == v for k, v in prefs.items()):
                self.state["applied_results"].append(request.result_id)
                return {"applied": True, "reused": True, "trip": self.state["trip"], "version": self.state["version"]}
            receipt = await self.runtime.store.call("apply", self.scope, self.owner,
                fingerprint({"result": request.result_id, "trip": trip, "preferences": prefs, "action": action}),
                result.input_version, trip, prefs, action)
            self.state["trip"], self.state["version"] = receipt["trip"], receipt["version"]
            if action in {"new", "cancel"}:
                self.state["work_context"] = {}
            self.state["preferences_updated"] |= receipt["preferences_updated"]
            self.state["results"][request.result_id]["input_version"] = receipt["version"]
            if request.result_id not in self.state["applied_results"]:
                self.state["applied_results"].append(request.result_id)
            return receipt
        if name == "finish":
            if request.pending_input and request.kind not in {"ask", "clarify"}:
                raise ToolRejected("只有提问可以声明待回答字段或选项")
            if request.kind == "clarify":
                if request.result_ids or not request.question.strip():
                    raise ToolRejected("意图澄清必须填写问题且不选择业务结果")
                return {"_terminal": clarification_output(request.question, request.pending_input)}
            selected = self.results(request.result_ids)
            if request.kind == "answer" and selected and all(r.status in {"unavailable", "error"} for r in selected):
                return {"_terminal": self.fallback("NO_PROGRESS")}
            if request.kind != "refuse":
                for result in selected:
                    proposal = result.role == "trip_context" and bool(result.data.get("trip") or result.data.get("trip_action", "update") != "update")
                    proposal |= result.role == "memory" and bool(result.data.get("preferences"))
                    if proposal and result.result_id not in self.state["applied_results"]:
                        raise ToolRejected("结果包含尚未提交的变更，请先 apply_changes；无法验证时重做专业任务，不要声称已保存")
            # Field readiness owns the collection UI, including malformed ask
            # calls. A missing model-authored question must not suppress a form
            # that the runtime can already build from committed state.
            if request.kind != "refuse" and not evaluate_trip_intake(self.state["trip"])["planning_ready"]:
                candidates = selected or self.deliverable_results()
                if can_show_intake(self.state, candidates):
                    return {"_terminal": {**self.intake(), "outcome": "waiting_input"}}
            if request.kind == "answer" and not selected:
                raise ToolRejected("答案必须选择至少一个专业结果")
            if request.kind == "ask" and not request.question.strip():
                raise ToolRejected("请填写需要用户补充的问题")
            if request.kind == "answer" and request.question:
                raise ToolRejected("不要在 question 中写答案")
            output = render(request, selected)
            if request.kind == "answer" and any(r.role in {"trip_context", "trip_planner"} for r in selected) and hasattr(self.runtime.services, "prepare_trip_options"):
                choices = await self.prepare_options()
                if choices.get("answer_document"):
                    from .full_trip import merge_documents
                    choices = merge_documents(choices, output)
                    if any(r.status != "success" or r.missing_info for r in selected):
                        choices["outcome"] = "partial"
                return {"_terminal": choices}
            if request.kind == "ask":
                output["outcome"] = "waiting_input"
                if request.pending_input:
                    output["_pending_input"] = {"question": request.question, "input": request.pending_input.model_dump()}
            return {"_terminal": output}
        raise ToolRejected("无效操作")

    async def delegate(self, parent, call, request):
        group = (request.role, self.state["version"])
        async with self.task_locks.setdefault(group, asyncio.Lock()):
            return await self._delegate(parent, call, request)

    async def _delegate(self, parent, call, request):
        guard = guard_user_input(request.task)
        if guard and guard.intent == "unsupported":
            raise ToolRejected("委派任务超出企业差旅范围")
        if request.role == "trip_context" and self.state.get("intake_submission_applied"):
            result = self.results(["result_intake_submission"])[0]
            return {**result.brief(), "reused": True, "committed": True}
        if request.role != "trip_context":
            if any(r["role"] == "trip_context" and (r["data"].get("trip") or r["data"].get("trip_action", "update") != "update")
                   and r["result_id"] not in self.state["applied_results"] and r["result_id"] not in self.state["discarded_results"]
                   for r in self.state["results"].values()):
                raise ToolRejected("本轮行程修订尚未提交，请先 apply_changes 再使用行程", code="UNCOMMITTED_TRIP", next_action="apply_changes")
        if request.role == "trip_planner":
            intake = evaluate_trip_intake(self.state["trip"])
            if not intake["planning_ready"]:
                raise ToolRejected("生成具体行程前需补充或澄清：" + "、".join(intake["missing_required"]),
                                   code="TRIP_INCOMPLETE", next_action="ask_user", fields=intake["missing_required"])
            # Auto-wire the current validated policy/travel facts when the
            # model omits dependency IDs. Never plan from a task's prose alone.
            ids = list(request.result_ids)
            for role in ("policy_rag", "travel_info"):
                if any(self.state["results"].get(i, {}).get("role") == role for i in ids):
                    continue
                candidates = []
                for key, raw_result in self.state["results"].items():
                    if raw_result["role"] != role:
                        continue
                    try:
                        candidate = self.results([key])[0]
                        if candidate.status in {"success", "partial"} and candidate.evidence_refs:
                            candidates.append(candidate.result_id)
                    except ToolRejected:
                        continue
                if candidates:
                    ids.append(candidates[-1])
            request = Delegate.model_validate({**request.model_dump(), "result_ids": ids})
        dependencies = self.results(request.result_ids)
        operation = task_key(request, self.state["version"])
        ledger = self.state.setdefault("work_items", {})
        previous = ledger.get(operation)
        if previous and previous["status"] in {"completed", "partial", "needs_input"} and previous["result_id"] not in self.state["discarded_results"]:
            try:
                result = self.results([previous["result_id"]])[0]
                return {**result.brief(), "reused": True}
            except ToolRejected:
                pass
        # Paraphrasing a failed task cannot bypass this per-role/version cap.
        group_items = [w for w in ledger.values() if w["role"] == request.role and w["input_version"] == self.state["version"] and w["status"] != "pending"]
        if previous and previous["status"] == "failed":
            raise ToolRejected("该任务已经失败；请交付其他结果或提出缺项，不重新查询。", code="TASK_ALREADY_FAILED", next_action="finish")
        # Include round in identity: providers may recycle tool IDs across rounds.
        key = f'{parent["round"]}:{call["id"]}'
        child = self.state["children"].get(key)
        if child and child["result_id"] in self.state["results"]:
            return SpecialistResult.model_validate(self.state["results"][child["result_id"]]).brief()
        if child is None:
            if len(group_items) >= self.runtime.config.get("max_role_tasks", 2):
                raise ToolRejected("本轮该类任务已达到执行上限；请交付已有结果。", code="ROLE_TASK_LIMIT", next_action="finish")
            if self.state["calls"] >= self.runtime.config["max_children"]:
                raise ToolRejected("本轮委派任务数量已达上限，请交付已完成结果")
            consume_agent_call(request.role)
            self.state["calls"] += 1
            result_id = call.get("step_id") or "result_" + uuid4().hex[:16]
            profile = PROFILES[request.role]
            # A leaf receives only current user text, a small trip snapshot, and
            # explicitly selected dependency results. No parent transcript/tool log.
            context = {"request": call.get("request_text", self.text), "task": request.task, "trip": self.state["trip"],
                "resolved_input": self.state.get("resolved_input"), "pending_input": self.state.get("reply_to"),
                "today": beijing_today(),
                "dependencies": [r.model_dump(exclude={"sources"}) for r in dependencies]}
            child = {"result_id": result_id, "role": request.role, "task_key": operation, "version": self.state["version"], "round": 0, "sources": [], "read_ids": [],
                "messages": [{"role": "system", "content": BASE_RULES + "\n" + profile.instructions + (POLICY_QUERY_RULES if request.role == "policy_rag" else "") + "\n" + call.get("skill_guidance", "")},
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)}]}
            self.state["children"][key] = child
            execution_plan.register(self.state, operation, WorkItem(role=request.role, task=request.task, input_version=self.state["version"],
                                        dependency_ids=request.result_ids, result_id=result_id).model_dump())
            await self.publish_plan()
        # Retain full dependency provenance for expiry checks, even if a planner
        # does not cite every input in its short user-facing summary.
        inherited = list({s["id"]: s for r in dependencies for s in r.sources}.values())
        sources = SourceScope([*inherited, *child.get("sources", [])])
        sources.read_ids.update(child.get("read_ids", []))
        started = time.perf_counter()
        await self.emit("queued", child["result_id"], request.role)
        async with self.semaphore:
            await self.start_step(child["result_id"])
            await self.emit("running", child["result_id"], request.role)
            try:
                raw = child.get("terminal") or await asyncio.wait_for(self.loop(child, request.role, sources, dependencies), timeout=self.runtime.config["child_timeout_sec"])
                report = Report.model_validate(raw)
            except RuntimeStopped:
                raise
            except ExecutionLimitExceeded as exc:
                if exc.code not in {"SUPERVISOR_CONTEXT_LIMIT", "SUPERVISOR_ROUND_LIMIT"}:
                    raise
                logger.warning("Specialist local budget exhausted role=%s code=%s", request.role, exc.code)
                report = Report(status="unavailable", summary="本次未能在查询预算内形成可核实的结论，已有其他查询结果仍可交付。")
                child["stop_reason"] = exc.code
                child.setdefault("last_error", failure(exc.code, report.summary, "finish")["failure"])
            except Exception as exc:
                logger.warning("Specialist failed role=%s error_type=%s", request.role, type(exc).__name__)
                report = Report(status="unavailable", summary="本次专业任务未完成，可交付其他已完成结果或稍后继续。")
                child["stop_reason"] = "CHILD_TIMEOUT" if isinstance(exc, asyncio.TimeoutError) else "CHILD_UNAVAILABLE"
                child.setdefault("last_error", failure(child["stop_reason"], report.summary, "finish")["failure"])
        result = SpecialistResult(**report.model_dump(), result_id=child["result_id"], role=request.role, task=request.task,
            input_version=child["version"], sources=list({s["id"]: s for s in [*inherited, *[sources.sources[i] for i in report.evidence_refs]]}.values()))
        self.state["results"][result.result_id] = result.model_dump()
        if result.role == "trip_context" and (result.data.get("trip") or result.data.get("trip_action", "update") != "update"):
            # A validated user-grounded proposal is committed by the runtime.
            # The receipt/fencing gate remains the sole writer; a model cannot
            # claim success while forgetting to call apply_changes.
            await self.invoke_main(parent, {"name": "apply_changes", "arguments": {"result_id": result.result_id}})
            result = SpecialistResult.model_validate(self.state["results"][result.result_id])
        if operation in ledger:
            execution_plan.complete(self.state, result.result_id)
            ledger[operation]["error_code"] = (child.get("last_error") or {}).get("code")
            if result.input_version != ledger[operation]["input_version"]:
                ledger[operation]["input_version"] = result.input_version
                ledger[task_key(request, result.input_version)] = ledger.pop(operation)
        if report.status not in {"error", "unavailable"}:
            self.state["work_context"][request.role] = {"task": request.task[:500], "summary": report.summary[:1000],
                "missing_info": report.missing_info, "version": result.input_version}
        await self.save()
        await self.publish_plan()
        await self.emit("completed" if report.status not in {"error", "unavailable"} else "failed", result.result_id, request.role)
        logger.info("Specialist completed role=%s status=%s duration=%.2f", request.role, report.status, time.perf_counter() - started)
        brief = {**result.brief(), "committed": result.result_id in self.state["applied_results"]}
        if report.status in {"error", "unavailable"} and (child.get("last_error") or {}).get("code") in {"EMPTY_TRIP_REPORT", "EMPTY_CHANGESET"}:
            brief["failure"] = {**child["last_error"], "next_action": "ask_user"}
        return brief
