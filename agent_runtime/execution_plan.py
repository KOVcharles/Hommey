"""Runtime-owned step transitions and a bounded public projection of work_items.

The model cannot call this API. Existing work_items remain the execution ledger;
public_plan is a disposable snapshot, never a second source of execution state.
"""
from copy import deepcopy
from datetime import datetime, timezone

from .contracts import ToolRejected

ACTIVE = {"pending", "running"}
LABELS = {
    "trip_context": ("整理并保存出差信息", "核实出发地、目的地、日期和出差目的"),
    "policy_rag": ("查询差旅标准", "确认适用的交通、住宿和报销要求"),
    "travel_info": ("整理天气与交通信息", "获取行程所需的实时信息"),
    "memory": ("核对差旅记录与偏好", "使用已有记录减少重复填写"),
    "trip_planner": ("生成出差行程", "结合已核实的信息安排日程"),
    "compliance": ("检查差旅合规", "核对方案与制度要求"),
}


def now():
    return datetime.now(timezone.utc).isoformat()


def register(state, key, item, *, title=None, purpose=None):
    ledger = state.setdefault("work_items", {})
    existing = next((v for v in ledger.values() if v["result_id"] == item["result_id"]), None)
    if existing:
        return existing
    title_default, purpose_default = LABELS[item["role"]]
    item = {**item, "status": "pending", "step_id": item["result_id"],
            "title": title or title_default, "purpose": purpose or purpose_default,
            "summary": "", "started_at": None, "finished_at": None}
    ledger[key] = item
    return item


def transition(state, step_id, target, *, reason=None):
    item = next(v for v in state["work_items"].values() if v["result_id"] == step_id)
    current = item["status"]
    if current == target:
        return
    allowed = {"pending": {"running", "skipped", "cancelled", "failed"},
               "running": {"completed", "partial", "needs_input", "failed", "cancelled"}}
    if target not in allowed.get(current, set()):
        raise ToolRejected("步骤状态转换不合法", code="INVALID_STEP_TRANSITION", next_action="finish")
    if target == "running":
        for dep in item.get("dependency_ids", []):
            result = state["results"].get(dep)
            if not result or result["status"] not in {"success", "partial"} or dep in state.get("discarded_results", []):
                raise ToolRejected("前置步骤没有可用结果", code="DEPENDENCY_NOT_READY", next_action="finish")
        item["started_at"] = now()
    if target in {"completed", "partial", "needs_input"}:
        result = state["results"].get(step_id)
        expected = {"completed": "success", "partial": "partial", "needs_input": "needs_input"}[target]
        if not result or result["status"] != expected:
            raise ToolRejected("缺少通过校验的步骤结果", code="STEP_RESULT_REQUIRED", next_action="finish")
        proposal = result["role"] == "trip_context" and bool(result["data"].get("trip") or result["data"].get("trip_action", "update") != "update")
        if proposal and step_id not in state.get("applied_results", []):
            raise ToolRejected("行程尚未保存", code="STEP_COMMIT_REQUIRED", next_action="apply_changes")
    item["status"] = target
    item["summary"] = reason or {
        "running": "正在处理此步骤。", "completed": "已完成校验。", "partial": "已有可核实结果，仍有信息缺失。",
        "needs_input": "需要补充信息后继续。", "failed": "此步骤未完成，其他有效结果仍可交付。",
        "cancelled": "本次执行已停止。", "skipped": "本轮不再执行此步骤。",
    }[target]
    if target not in ACTIVE:
        item["finished_at"] = now()


def complete(state, step_id):
    result = state["results"][step_id]
    target = {"success": "completed", "partial": "partial", "needs_input": "needs_input"}.get(result["status"], "failed")
    transition(state, step_id, target)


def settle(state, outcome, reason=None):
    for item in state.get("work_items", {}).values():
        if item["status"] in ACTIVE:
            target = "cancelled" if outcome == "cancelled" else "skipped" if outcome == "waiting_input" else "failed"
            transition(state, item["result_id"], target)
            item["error_code"] = reason
    statuses = {w["status"] for w in state.get("work_items", {}).values()}
    if outcome == "completed":
        if "failed" in statuses:
            outcome = "degraded"
        elif "needs_input" in statuses:
            outcome = "waiting_input"
        elif "partial" in statuses:
            outcome = "partial"
    return outcome


def resume(state):
    # A new fenced owner may continue a cancelled request. This is an explicit
    # bounded recovery transition, not a model-requested retry or a new step.
    child_ids = {c["result_id"] for c in state.get("children", {}).values()}
    for item in state.get("work_items", {}).values():
        if item["status"] == "cancelled" and item["result_id"] in child_ids:
            if item.get("attempts", 1) >= 2:
                item.update(status="failed", error_code="STEP_RESUME_LIMIT")
            else:
                item.update(status="pending", attempts=item.get("attempts", 1) + 1,
                            summary="从已保存的检查点继续。", started_at=None, finished_at=None)


def snapshot(state, run_id, *, outcome="running", reason=None):
    steps = []
    for item in state.get("work_items", {}).values():
        title, purpose = LABELS[item["role"]]
        steps.append({"step_id": item["result_id"], "title": item.get("title") or title,
                      "purpose": item.get("purpose") or purpose, "depends_on": item.get("dependency_ids", []),
                      "status": "succeeded" if item["status"] == "completed" else item["status"],
                      "summary": item.get("summary", "已复用通过校验的历史结果。"),
                      "started_at": item.get("started_at"), "finished_at": item.get("finished_at")})
    old = state.get("public_plan", {})
    # Reserve the intervening revision for a read projection of a DB-fenced
    # stop; a subsequent owner must publish a newer revision when resuming.
    plan = {"run_id": run_id, "revision": old.get("revision", 0) + 2,
            "status": outcome, "change_reason": reason or ("已登记待执行步骤。" if steps else "正在确认需求。"),
            "steps": steps}
    state["public_plan"] = plan
    return deepcopy(plan)
