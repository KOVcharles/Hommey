"""Deterministic write/evidence gates, independent of agent instructions."""
from __future__ import annotations

from datetime import date, timedelta
import re

from context.preference_schema import PREFERENCE_LIST_COLUMNS, PREFERENCE_SCALAR_COLUMNS
from core.trip_intake import beijing_today
from utils.memory_safety import is_safe_preference_value
from .contracts import ToolRejected


TRIP_FIELDS = {"origin", "destination", "start_date", "end_date", "duration_days", "trip_purpose", "work_location", "work_schedule"}


def grounded_date(value, quote):
    parsed = date.fromisoformat(value)
    today = date.fromisoformat(beijing_today())
    if value in quote or f"{parsed.year}年{parsed.month}月{parsed.day}日" in quote:
        return True
    match = re.search(r"(?:(\d{4})[年/.-])?(\d{1,2})[月/.-](\d{1,2})(?:日|号)?", quote)
    if match:
        year = int(match[1]) if match[1] else today.year
        candidate = date(year, int(match[2]), int(match[3]))
        return parsed == candidate
    for word, days in (("大后天", 3), ("后天", 2), ("明天", 1), ("今天", 0)):
        if word in quote:
            return parsed == today + timedelta(days=days)
    match = re.search(r"(下下|下|本|这)周([一二三四五六日天])", quote)
    if match:
        weeks = {"下下": 2, "下": 1, "本": 0, "这": 0}[match[1]]
        day = "一二三四五六日".find(match[2].replace("天", "日"))
        return parsed == today + timedelta(days=weeks * 7 + day - today.weekday())
    return False


def validated_changes(result, user_text, current_trip, current_preferences):
    if result.status not in {"success", "partial", "needs_input"}:
        raise ToolRejected("失败结果不能提交变更")
    trip = result.data.get("trip", {}) if result.role == "trip_context" else {}
    preferences = result.data.get("preferences", {}) if result.role == "memory" else {}
    action = result.data.get("trip_action", "update") if result.role == "trip_context" else "update"
    if action not in {"new", "update", "cancel"}:
        raise ToolRejected("无效的行程操作")
    if action != "update":
        quote = result.data.get("action_source", "")
        if not isinstance(quote, str) or not quote or quote not in user_text:
            raise ToolRejected("替换现有行程或取消行程需要 action_source 用户原文；仅补充当前行程请用 trip_action=update", code="ACTION_NOT_AUTHORIZED", fields=["data.trip_action", "data.action_source"])
        if action == "new" and not any(word in quote for word in ("新行程", "新的出差", "新出差", "另一次出差", "重新开始")):
            raise ToolRejected("用户未明确要求替换现有行程；应使用 trip_action=update 修改当前行程", code="ACTION_NOT_AUTHORIZED", fields=["data.trip_action"])
        if action == "cancel" and not ("取消" in quote and any(word in quote for word in ("出差", "行程"))):
            raise ToolRejected("缺少明确取消出差的依据")
        if action == "cancel" and (trip or preferences):
            raise ToolRejected("取消行程不能同时修改其他字段")
        if action == "cancel" and not current_trip:
            raise ToolRejected("当前会话没有可取消的出差行程")
    if not isinstance(trip, dict) or not isinstance(preferences, dict) or not (trip or preferences or action != "update"):
        raise ToolRejected("该结果没有可提交的行程或偏好变更", code="EMPTY_CHANGESET", next_action="ask_user", fields=["data.trip", "data.preferences"])
    if action == "new":
        current_trip = {}
    if set(trip) - TRIP_FIELDS or set(preferences) - (PREFERENCE_SCALAR_COLUMNS.keys() | PREFERENCE_LIST_COLUMNS.keys()):
        raise ToolRejected("变更包含未授权字段")
    for values, sources_key, old in ((trip, "field_sources", current_trip), (preferences, "preference_sources", current_preferences)):
        quotes = result.data.get(sources_key, {})
        if not isinstance(quotes, dict):
            raise ToolRejected("缺少字段来源")
        for key, value in values.items():
            if value == old.get(key):
                continue
            quote = quotes.get(key)
            if not isinstance(quote, str) or not quote.strip() or quote not in user_text:
                raise ToolRejected(f"{key} 缺少本轮用户明确表达的原文依据")
            if not is_safe_preference_value(value):
                raise ToolRejected("变更包含不允许持久保存的敏感内容")
            if key in {"start_date", "end_date"}:
                if not isinstance(value, str) or not grounded_date(value, quote):
                    raise ToolRejected(f"{key} 不能从原文确定，请补问明确日期")
            elif key == "duration_days":
                if type(value) is not int or not 1 <= value <= 90:
                    raise ToolRejected("出差天数必须是 1 到 90 的整数")
                chinese = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                if not re.search(rf"(?<!\d){value}\s*天", quote) and not any(n == value and k + "天" in quote for k, n in chinese.items()):
                    raise ToolRejected("出差天数缺少明确依据")
            else:
                parts = value if key in PREFERENCE_LIST_COLUMNS else [value]
                if not isinstance(parts, list) or len(parts) > 12:
                    raise ToolRejected("偏好必须是有界文本列表")
                previous = old.get(key, [])
                previous = previous if isinstance(previous, list) else [previous]
                if any(not isinstance(p, str) or not p or len(p) > 500 or (p not in quote and p not in previous) for p in parts):
                    raise ToolRejected(f"{key} 的值必须来自用户原文；不要推断或改写")
                if key in PREFERENCE_LIST_COLUMNS and not isinstance(value, list):
                    raise ToolRejected("品牌/航司偏好必须是文本数组")
    merged = {**current_trip, **trip}
    if merged.get("start_date") and merged.get("end_date"):
        start, end = date.fromisoformat(merged["start_date"]), date.fromisoformat(merged["end_date"])
        if end < start or (end - start).days >= 90:
            raise ToolRejected("返程日期必须在出发日期之后且行程不超过 90 天")
        if merged.get("duration_days") and merged["duration_days"] != (end - start).days + 1:
            raise ToolRejected("出差天数和起止日期冲突，请先澄清")
    return trip, preferences, action


def validate_report(role, report, source_scope, dependencies):
    known = source_scope.sources
    if set(report.evidence_refs) - known.keys():
        raise ToolRejected("报告引用了本任务不存在的来源", code="UNKNOWN_EVIDENCE", fields=["evidence_refs"])
    if role in {"policy_rag", "memory", "travel_info"} and not set(report.evidence_refs) <= source_scope.read_ids:
        raise ToolRejected("报告引用的来源尚未回读；不要把检索摘要作为已核实证据", code="UNREAD_EVIDENCE", fields=["evidence_refs"])
    if role in {"policy_rag", "memory", "travel_info"} and report.evidence_refs:
        from .contracts import PolicyData
        if not report.data.get("findings"):
            raise ToolRejected("请在 data.findings 中提供具体标准或事实、适用条件及来源，不能只说已查到资料",
                               code="MISSING_FINDINGS", fields=["data.findings"])
        findings = PolicyData.model_validate({"findings": report.data["findings"]}).findings
        if any(not set(item.evidence_refs) <= set(report.evidence_refs) for item in findings):
            raise ToolRejected("每条事实的 evidence_refs 必须包含在报告引用中", code="INVALID_FINDING_EVIDENCE", fields=["data.findings.evidence_refs"])
        if any(not item.evidence_refs for item in findings):
            raise ToolRejected("确定事实必须提供直接来源，未知事项应放入 missing_info", code="MISSING_EVIDENCE", fields=["data.findings.evidence_refs"])
    if role in {"policy_rag", "memory", "travel_info"} and report.status == "success":
        # An explicit preference proposal requires no retrieval. Other factual
        # retrieval answers must retain an actually read source.
        preference_only = role == "memory" and bool(report.data.get("preferences"))
        if not preference_only and (not report.evidence_refs or not set(report.evidence_refs) <= source_scope.read_ids):
            raise ToolRejected("成功报告缺少 evidence_refs；请填写已读来源 ID，无证据时返回 unavailable，不要重复读取已经读过的来源",
                               code="MISSING_EVIDENCE", fields=["evidence_refs"])
    if role == "compliance":
        verdict = report.data.get("verdict", "unknown")
        if verdict not in {"compliant", "non_compliant", "partial", "unknown"}:
            raise ToolRejected("合规结论格式不正确")
        if verdict != "unknown":
            if not report.data.get("checks"):
                raise ToolRejected("确定合规结论必须提供逐项检查", code="MISSING_CHECKS", fields=["data.checks"])
            if not any(r.role == "policy_rag" and r.evidence_refs for r in dependencies) or not report.evidence_refs:
                raise ToolRejected("缺少政策证据，合规结论只能为 unknown")
            for item in report.data.get("checks", []):
                if not isinstance(item, dict) or set(item.get("evidence_refs", [])) - known.keys():
                    raise ToolRejected("合规检查项引用无效")
