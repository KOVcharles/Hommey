"""Resolve a user turn against durable workflow state before normal routing."""
from __future__ import annotations

from dataclasses import dataclass

from core.intent_router import FastIntentRouter


_CONTINUE = {
    "继续", "接着做", "继续执行", "继续上次任务", "接着上次做", "恢复任务",
    "继续吧", "接着来", "继续规划",
}


@dataclass(frozen=True)
class TurnRelation:
    kind: str
    reason: str


class TurnResolver:
    @staticmethod
    def resolve(message: str, state) -> TurnRelation:
        normalized = "".join((message or "").strip().lower().split())
        if normalized in _CONTINUE:
            return TurnRelation("resume", "explicit continuation")

        if state.status == "WAITING_USER":
            if TurnResolver._explicit_new_goal(message):
                return TurnRelation("new_goal", "user explicitly starts another goal")
            candidates = FastIntentRouter.detect(message)
            active_intents = {goal.intent for goal in state.goals.values()}
            new_intents = {item.type for item in candidates} - active_intents
            if new_intents and not TurnResolver._looks_like_slot_update(message):
                return TurnRelation("new_goal", "clear independent intent while workflow waits")
            if TurnResolver._looks_like_slot_update(message):
                return TurnRelation("resume", "input belongs to the active collection prompt")
            return TurnRelation("new_goal", "ambiguous non-slot input requires full intent classification")

        if state.status in {"INTERRUPTED", "INTERRUPTING"}:
            if TurnResolver._explicit_new_goal(message):
                return TurnRelation("new_goal", "user explicitly starts another goal")
            candidates = FastIntentRouter.detect(message)
            active_intents = {goal.intent for goal in state.goals.values()}
            if (
                any(item.type in active_intents for item in candidates)
                or TurnResolver._looks_like_same_intent(message, active_intents)
            ):
                return TurnRelation("resume", "input revises an interrupted goal of the same intent")
            if TurnResolver._looks_like_task_revision(message):
                return TurnRelation("resume", "follow-up revises the interrupted task")
            return TurnRelation("new_goal", "no explicit continuation")

        return TurnRelation("new_goal", "active run is not waiting for this turn")

    @staticmethod
    def _looks_like_slot_update(message: str) -> bool:
        text = (message or "").strip()
        query_terms = (
            "哪里", "什么", "怎么", "为何", "为什么", "多少", "查", "查询",
            "上次", "历史", "天气", "标准", "政策", "报销", "偏好", "记得",
            "谢谢", "感谢", "你好", "您好", "好的", "没事", "算了", "再见",
        )
        if any(term in text for term in query_terms):
            return False
        terms = (
            "出发", "从", "目的地", "去", "日期", "明天", "后天", "下周",
            "天", "客户", "培训", "会议", "拜访", "工作地点",
        )
        if any(term in text for term in terms):
            return True
        # “北京”“三天”“客户拜访”等短值可以直接回答收集卡；带疑问/查询词的
        # 短问题已在上面排除，不能再被长度规则误吞。
        return 1 <= len(text) <= 8 and not any(mark in text for mark in "？?!！")

    @staticmethod
    def _looks_like_task_revision(message: str) -> bool:
        terms = ("改成", "修改", "补充", "出发", "目的地", "日期", "酒店", "航班", "行程")
        return any(term in (message or "") for term in terms)

    @staticmethod
    def _looks_like_same_intent(message: str, active_intents: set[str]) -> bool:
        text = message or ""
        hints = {
            "information_query": ("天气", "交通", "航班", "高铁", "火车", "机场"),
            "memory_query": ("上次", "历史", "去过", "记得", "以前"),
            "rag_knowledge": ("标准", "政策", "制度", "报销", "补贴"),
            "itinerary_planning": ("行程", "规划", "安排", "路线", "住宿"),
            "preference": ("偏好", "喜欢", "常坐", "常住"),
            "event_collection": ("出发", "目的地", "日期", "出差", "会议", "拜访"),
            "trip_compliance": ("合规", "符合", "超标", "违规"),
        }
        return any(
            intent in active_intents and any(term in text for term in terms)
            for intent, terms in hints.items()
        )

    @staticmethod
    def _explicit_new_goal(message: str) -> bool:
        text = message or ""
        markers = (
            "另外", "另一个", "另一趟", "新的行程", "新任务",
            "再规划一个", "再查一个", "顺便再", "同时再",
        )
        return any(marker in text for marker in markers)
