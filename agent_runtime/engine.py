"""One bounded supervisor loop, six isolated leaf profiles, durable tool boundaries."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from uuid import uuid4

from pydantic import ValidationError

from core.execution_budget import ExecutionLimitExceeded, consume_agent_call
from core.intent_guard import guard_user_input
from core.trip_intake import beijing_today, evaluate_trip_intake
from utils.io_executor import run_blocking
from .contracts import (
    ApplyChanges, Delegate, Finish, ReadResult, ReadSkill, Report, RuntimeStopped,
    SpecialistResult, ToolRejected, schema,
)
from .model_client import assistant_message, call_model, tool_message
from .profiles import BASE_RULES, MAIN_RULES, PROFILES
from .render import REFUSAL, render
from .services import SourceScope, TOOLS, tool_schemas
from .store import fingerprint, trip_version
from .validation import validate_report, validated_changes


logger = logging.getLogger(__name__)
MAIN_TOOLS = [
    schema("delegate", "委派一个独立专业任务；result_ids 只填已完成的依赖，可同轮并行委派", Delegate),
    schema("read_result", "按 ID 查看专业结果结构和证据引用，不读取原始检索日志", ReadResult),
    schema("discard_result", "丢弃无效或过时结果，解除其尚未提交的变更；已经提交的写入不会撤销", ReadResult),
    schema("read_skill", "读取允许的差旅 Skill 业务指南", ReadSkill),
    schema("apply_changes", "提交有本轮用户原文依据的行程/偏好变更；只接受专业结果 ID", ApplyChanges),
    schema("finish", "结束本轮，选择有据结果或提出缺项问题；非差旅请求 refuse", Finish),
]
MODELS = {"delegate": Delegate, "read_result": ReadResult, "discard_result": ReadResult, "read_skill": ReadSkill, "apply_changes": ApplyChanges, "finish": Finish}


class Supervisor:
    def __init__(self, model, services, store, config):
        self.model, self.services, self.store, self.config = model, services, store, config

    async def run(self, scope, text, *, user_text=None, progress=None):
        run = Turn(self, scope, text, user_text if user_text is not None else text, progress)
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

    async def save(self, response=None, status="running"):
        async with self.lock:
            snapshot = deepcopy(self.state)
            if len(json.dumps(snapshot, ensure_ascii=False, default=str)) > 1_000_000:
                raise ExecutionLimitExceeded("SUPERVISOR_CONTEXT_LIMIT", "本次任务上下文达到上限，请缩小任务范围")
            await self.runtime.store.call("save", self.scope, self.owner, snapshot, response, status)

    async def emit(self, phase, result_id=None):
        if self.progress:
            keys = {"analyzing": "request_analyzing", "queued": "queued", "running": "task_running",
                "completed": "task_completed", "failed": "task_failed", "done": "answer_ready"}
            await self.progress({"type": "task_status" if result_id else "status", "phase": phase,
                "message_key": keys.get(phase, "task_running"), "task_id": result_id})

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
                # Only a compact same-session work index. Raw historical sources
                # are deliberately not inherited; memory must retrieve them.
                if previous:
                    checkpoint = previous.get("checkpoint") or {}
                    context["previous_work"] = {"status": previous["status"], "results": [
                        {"role": r["role"], "summary": r["summary"][:500], "missing_info": r.get("missing_info", [])}
                        for r in list(checkpoint.get("results", {}).values())[-6:]]}
                    context["work_context"] = checkpoint.get("work_context", {})
                self.state = {"trip": context["trip"], "version": trip_version(context["trip"]), "results": {},
                    "work_context": context.get("work_context", {}),
                    "children": {}, "calls": 0, "preferences_updated": False, "applied_results": [], "discarded_results": [],
                    "main": {"messages": [{"role": "system", "content": MAIN_RULES},
                        {"role": "user", "content": json.dumps({"context": context, "current_request": self.text}, ensure_ascii=False, default=str)}], "round": 0}}
                await self.save()
            guard = guard_user_input(self.user_text, conversation_context="当前企业差旅会话")
            if guard and guard.intent == "unsupported":
                output = render(Finish(kind="refuse"), [])
            elif guard and guard.intent == "chitchat":
                output = {"response": "你好，我可以帮你查询企业差旅制度、整理行程、查询交通天气和个人差旅记录。", "answer_document": None, "presentation_document": None}
            else:
                await self.emit("analyzing")
                # DB cancellation works across workers, including during model waits.
                work = asyncio.create_task(self.loop(self.state["main"], None, None, []))
                monitor = asyncio.create_task(self.monitor())
                try:
                    done, _ = await asyncio.wait({work, monitor}, return_when=asyncio.FIRST_COMPLETED)
                    if monitor in done:
                        await monitor
                    output = await work
                finally:
                    work.cancel()
                    monitor.cancel()
                    await asyncio.gather(work, monitor, return_exceptions=True)
            output.update({"agents": [{"name": r["role"], "display": PROFILES[r["role"]].title, "status": r["status"], "duration_sec": 0}
                for r in self.state["results"].values()], "preferences_updated": self.state["preferences_updated"], "engine": "supervisor"})
            await self.save(output, "completed")
            await self.emit("done")
            return output
        except RuntimeStopped:
            await self.runtime.store.call("stop", self.scope, self.owner, "interrupted")
            return {"response": "", "answer_document": None, "presentation_document": None, "agents": [],
                "preferences_updated": self.state.get("preferences_updated", False), "interrupted": True, "engine": "supervisor"}
        except BaseException as exc:
            try:
                await self.runtime.store.call("stop", self.scope, self.owner, "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed")
            except Exception:
                logger.exception("Unable to record supervisor stop")
            raise

    async def monitor(self):
        while True:
            await self.runtime.store.call("check", self.scope, self.owner)
            await asyncio.sleep(0.5)

    async def loop(self, state, role, sources, dependencies):
        if "terminal" in state:
            return state["terminal"]
        max_rounds = self.runtime.config["main_rounds" if role is None else "child_rounds"]
        tools = MAIN_TOOLS if role is None else tool_schemas(PROFILES[role].tools) + [
            schema("read_skill", "读取本角色允许的业务 Skill", ReadSkill), schema("report", "提交有据摘要及结构化数据", Report)]
        while state["round"] < max_rounds or state.get("pending"):
            await self.runtime.store.call("check", self.scope, self.owner)
            if not state.get("pending"):
                if len(json.dumps(state["messages"], ensure_ascii=False, default=str)) > 80000:
                    raise ExecutionLimitExceeded("SUPERVISOR_CONTEXT_LIMIT", "本次任务资料过多，请缩小问题范围后继续")
                state["round"] += 1
                try:
                    reply = await call_model(self.runtime.model, state["messages"], tools)
                except ToolRejected:
                    state["messages"].append({"role": "user", "content": "上次工具调用参数无效，请输出完整有效的原生工具调用。"})
                    await self.save()
                    continue
                if not reply.calls:
                    state["messages"].append({"role": "user", "content": "请使用提供的原生工具，完成后调用 finish 或 report。"})
                    await self.save()
                    continue
                if len(reply.calls) > 6:
                    raise ToolRejected("同轮工具调用超过上限")
                state["pending"] = reply.calls
                state["outputs"] = {}
                state["messages"].append(assistant_message(reply.calls))
                await self.save()  # Before any tool execution: replay knows pending calls.
            calls = state["pending"]
            # Parallelize only independent leaf delegation. Mutations and finish
            # must be issued alone, avoiding same-response ordering ambiguity.
            exclusive = any(c["name"] in {"finish", "report", "apply_changes"} for c in calls)
            if exclusive and len(calls) != 1:
                values = [{"error": "finish、report、apply_changes 必须单独一轮调用"} for _ in calls]
            elif role is None and all(c["name"] == "delegate" for c in calls):
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
            state.pop("pending", None)
            state.pop("outputs", None)
            # Terminal value persists with its tool reply, so a retry does not
            # rerun a completed report/finish after a lost HTTP connection.
            if len(values) == 1 and isinstance(values[0], dict) and "_terminal" in values[0]:
                state["terminal"] = values[0]["_terminal"]
            await self.save()
            if "terminal" in state:
                return state["terminal"]
        raise ExecutionLimitExceeded("SUPERVISOR_ROUND_LIMIT", "本次任务达到处理轮数上限，请缩小范围后继续")

    async def invoke_cached(self, state, call, role, sources, dependencies):
        if call["id"] in state["outputs"]:
            return state["outputs"][call["id"]]
        try:
            value = await self.invoke(state, call, role, sources, dependencies)
        except (ToolRejected, ValidationError, ValueError) as exc:
            # Pydantic errors may embed inputs; only expose bounded validation locations.
            message = "参数不符合工具格式，请检查字段和类型" if isinstance(exc, ValidationError) else str(exc)[:300]
            value = {"error": message}
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
                if source.get("kind") in {"train", "weather", "hotel", "commute"}:
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
            path = Path(__file__).resolve().parents[1] / ".agents/skills" / request.name / "SKILL.md"
            return {"skill": request.name, "guidance": (await run_blocking(path.read_text, encoding="utf-8"))[:12000]}
        if role is not None:
            if name == "report":
                request = Report.model_validate(raw)
                if len(json.dumps(request.data, ensure_ascii=False)) > 12000:
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
                return sources.read(request.source_id)
            try:
                kind, data = await asyncio.wait_for(self.runtime.services.execute(self.scope, name, request), timeout=self.runtime.config["tool_timeout_sec"])
            except (ExecutionLimitExceeded, RuntimeStopped, ToolRejected):
                raise
            except Exception as exc:
                logger.warning("Specialist tool unavailable role=%s tool=%s error_type=%s", role, name, type(exc).__name__)
                return {"status": "unavailable", "message": "该数据源本次查询不可用，请如实说明或缩小查询范围"}
            entries = data if kind in {"policy", "memory"} and isinstance(data, list) else [data]
            return {"sources": [sources.add(kind, item) for item in entries[:20]], "empty": not entries}
        if name not in MODELS:
            raise ToolRejected("主 Agent 只能委派、审阅和提交已验证结果")
        request = MODELS[name].model_validate(raw)
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
            preferences = await run_blocking(self.runtime.services.memory.long_term.get_preference)
            trip, prefs, action = validated_changes(result, self.user_text, self.state["trip"], preferences)
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
            selected = self.results(request.result_ids)
            if request.kind != "refuse":
                for result in selected:
                    proposal = result.role == "trip_context" and (result.data.get("trip") or result.data.get("trip_action", "update") != "update")
                    proposal |= result.role == "memory" and bool(result.data.get("preferences"))
                    if proposal and result.result_id not in self.state["applied_results"]:
                        raise ToolRejected("结果包含尚未提交的变更，请先 apply_changes；无法验证时重做专业任务，不要声称已保存")
            if request.kind == "answer" and not selected:
                raise ToolRejected("答案必须选择至少一个专业结果")
            if request.kind == "ask" and not request.question.strip():
                raise ToolRejected("请填写需要用户补充的问题")
            if request.kind == "answer" and request.question:
                raise ToolRejected("不要在 question 中写答案")
            if request.kind == "ask" and len(selected) == 1 and selected[0].role == "trip_context" and self.state["trip"]:
                from core.presentation.trip_intake_document import build_trip_intake_document
                if not evaluate_trip_intake(self.state["trip"])["planning_ready"]:
                    document = build_trip_intake_document(self.state["trip"]).model_dump(mode="json")
                    return {"_terminal": {"response": document["plain_text"], "answer_document": None, "presentation_document": document}}
            return {"_terminal": render(request, selected)}
        raise ToolRejected("无效操作")

    async def delegate(self, parent, call, request):
        guard = guard_user_input(request.task, conversation_context="当前企业差旅委派任务")
        if guard and guard.intent == "unsupported":
            raise ToolRejected("委派任务超出企业差旅范围")
        if request.role in {"travel_info", "trip_planner", "compliance"}:
            if any(r["role"] == "trip_context" and (r["data"].get("trip") or r["data"].get("trip_action", "update") != "update")
                   and r["result_id"] not in self.state["applied_results"] and r["result_id"] not in self.state["discarded_results"]
                   for r in self.state["results"].values()):
                raise ToolRejected("本轮行程修订尚未提交，请先审阅 apply_changes 再使用行程")
        if request.role == "trip_planner":
            intake = evaluate_trip_intake(self.state["trip"])
            if not intake["planning_ready"]:
                raise ToolRejected("生成具体行程前需补充或澄清：" + "、".join(intake["missing_required"]))
        dependencies = self.results(request.result_ids)
        # Include round in identity: providers may recycle tool IDs across rounds.
        key = f'{parent["round"]}:{call["id"]}'
        child = self.state["children"].get(key)
        if child and child["result_id"] in self.state["results"]:
            return SpecialistResult.model_validate(self.state["results"][child["result_id"]]).brief()
        if child is None:
            if self.state["calls"] >= self.runtime.config["max_children"]:
                raise ToolRejected("本轮委派任务数量已达上限，请交付已完成结果")
            consume_agent_call(request.role)
            self.state["calls"] += 1
            result_id = "result_" + uuid4().hex[:16]
            profile = PROFILES[request.role]
            # A leaf receives only current user text, a small trip snapshot, and
            # explicitly selected dependency results. No parent transcript/tool log.
            context = {"request": self.text, "task": request.task, "trip": self.state["trip"],
                "today": beijing_today(),
                "dependencies": [r.model_dump(exclude={"sources"}) for r in dependencies]}
            child = {"result_id": result_id, "version": self.state["version"], "round": 0, "sources": [], "read_ids": [],
                "messages": [{"role": "system", "content": BASE_RULES + "\n" + profile.instructions},
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)}]}
            self.state["children"][key] = child
            await self.save()
        # Retain full dependency provenance for expiry checks, even if a planner
        # does not cite every input in its short user-facing summary.
        inherited = list({s["id"]: s for r in dependencies for s in r.sources}.values())
        sources = SourceScope([*inherited, *child.get("sources", [])])
        sources.read_ids.update(child.get("read_ids", []))
        started = time.perf_counter()
        await self.emit("queued", child["result_id"])
        async with self.semaphore:
            await self.emit("running", child["result_id"])
            try:
                raw = child.get("terminal") or await asyncio.wait_for(self.loop(child, request.role, sources, dependencies), timeout=self.runtime.config["child_timeout_sec"])
                report = Report.model_validate(raw)
            except (RuntimeStopped, ExecutionLimitExceeded):
                raise
            except Exception as exc:
                logger.warning("Specialist failed role=%s error_type=%s", request.role, type(exc).__name__)
                report = Report(status="unavailable", summary="本次专业任务未完成，可交付其他已完成结果或稍后继续。")
        result = SpecialistResult(**report.model_dump(), result_id=child["result_id"], role=request.role, task=request.task,
            input_version=child["version"], sources=list({s["id"]: s for s in [*inherited, *[sources.sources[i] for i in report.evidence_refs]]}.values()))
        self.state["results"][result.result_id] = result.model_dump()
        if report.status not in {"error", "unavailable"}:
            self.state["work_context"][request.role] = {"task": request.task[:500], "summary": report.summary[:1000],
                "missing_info": report.missing_info, "version": child["version"]}
        await self.save()
        await self.emit("completed" if report.status not in {"error", "unavailable"} else "failed", result.result_id)
        logger.info("Specialist completed role=%s status=%s duration=%.2f", request.role, report.status, time.perf_counter() - started)
        return result.brief()
