"""跨轮"收集→暂停→续跑"检查点恢复契约。

- 第一轮 event_collection 信息不全 → 暂停 + 保存 checkpoint + trip_intake presentation。
- 第二轮补充仍不全 → 更新 checkpoint（事实合并）继续暂停。
- 补充齐全 → 从 steps_remaining 重建执行 → compose → 清除 checkpoint。
- checkpoint 不存在时 run_resume 退化为常规 run。
"""
import asyncio

import pytest

from core.orchestration.checkpoints import Checkpoint, CheckpointStore
from core.orchestration.pipeline import MultiIntentPipeline

PLAN_QUERY = "我明天去上海出差3天，帮我规划一下行程"
PLAN_INTENTION = {
    "intents": [{"type": "itinerary_planning", "confidence": 0.9, "should_call_skill": True}],
    "key_entities": {"destination": "上海", "date": "明天", "duration": "3天"},
    "rewritten_query": PLAN_QUERY,
}


def _store(tmp_path, user_id="u1"):
    return CheckpointStore(user_id=user_id, storage_dir=str(tmp_path))


def test_first_turn_pauses_and_saves_checkpoint(tmp_path):
    async def runner(**kwargs):
        if kwargs["agent_name"] == "event_collection":
            return {"status": "success", "data": {
                "destination": "上海", "start_date": "2026-08-08",
                "duration_days": 3, "trip_purpose": "客户拜访", "planning_ready": False,
            }}
        raise AssertionError(f"不应执行 {kwargs['agent_name']}")

    store = _store(tmp_path)
    pipeline = MultiIntentPipeline(model=None, composer_model=None, agent_runner=runner, checkpoint_store=store)
    output = asyncio.run(pipeline.run(PLAN_QUERY, PLAN_INTENTION, {}))

    assert output.paused is True
    assert output.answer_document is None
    assert output.presentation_document.type == "trip_intake"
    assert [p.key for p in output.presentation_document.missing_required] == ["origin"]

    checkpoint = asyncio.run(store.get())
    assert checkpoint is not None
    assert checkpoint.skill == "plan-trip"
    assert [s["agent_name"] for s in checkpoint.steps_remaining] == [
        "rag_knowledge", "information_query", "itinerary_planning", "trip_compliance",
    ]
    assert checkpoint.collected_facts["destination"] == "上海"


def test_resume_with_missing_info_updates_checkpoint_and_pauses(tmp_path):
    store = _store(tmp_path)
    asyncio.run(store.save(Checkpoint(
        user_id="u1", skill="plan-trip", request_id="",
        pause_agent="event_collection", pause_field="planning_ready",
        steps_remaining=[],  # 仅测试事实合并
        collected_facts={"destination": "上海", "start_date": "2026-08-08"},
        entities={"destination": "上海"},
    )))

    async def runner(**kwargs):
        if kwargs["agent_name"] == "event_collection":
            return {"status": "success", "data": {
                "destination": "上海", "start_date": "2026-08-09",
                "duration_days": 3, "trip_purpose": "客户拜访", "planning_ready": False,
            }}
        raise AssertionError(f"不应执行 {kwargs['agent_name']}")

    pipeline = MultiIntentPipeline(model=None, composer_model=None, agent_runner=runner, checkpoint_store=store)
    output = asyncio.run(pipeline.run_resume("后天出发", PLAN_INTENTION, {}))

    assert output.paused is True
    assert [p.key for p in output.presentation_document.missing_required] == ["origin"]
    checkpoint = asyncio.run(store.get())
    # 重跑 intake 后新事实合并进 checkpoint
    assert checkpoint.collected_facts["start_date"] == "2026-08-09"


def test_resume_completes_workflow_and_clears_checkpoint(tmp_path):
    store = _store(tmp_path)
    asyncio.run(store.save(Checkpoint(
        user_id="u1", skill="plan-trip", request_id="",
        pause_agent="event_collection", pause_field="planning_ready",
        steps_remaining=[
            {"skill": "ask-question", "agent_name": "rag_knowledge", "priority": 2,
             "on_failure": "abort", "max_retries": 1, "reason": "", "expected_output": "",
             "query": "查询{destination}出差适用的公司差旅标准"},
            {"skill": "plan-trip", "agent_name": "itinerary_planning", "priority": 3,
             "on_failure": "abort", "max_retries": 1, "reason": "", "expected_output": "",
             "query": "规划{origin}→{destination}合规差旅行程"},
        ],
        collected_facts={"origin": "北京", "destination": "上海", "start_date": "2026-08-08",
                         "duration_days": 3, "trip_purpose": "客户拜访"},
        entities={"destination": "上海"},
    )))

    seen_previous = {}

    async def runner(**kwargs):
        agent = kwargs["agent_name"]
        if agent == "event_collection":
            return {"status": "success", "data": {
                "origin": "北京", "destination": "上海", "start_date": "2026-08-08",
                "duration_days": 3, "trip_purpose": "客户拜访", "planning_ready": True,
            }}
        if agent == "rag_knowledge":
            seen_previous["rag"] = [p.get("agent_name") for p in kwargs.get("previous_results", [])]
            return {"status": "success", "data": {"answer": "住宿标准400元/晚。"}}
        if agent == "itinerary_planning":
            return {"status": "success", "data": {"itinerary": {"title": "上海出差方案"}}}
        raise AssertionError(f"不应执行 {agent}")

    pipeline = MultiIntentPipeline(model=None, composer_model=None, agent_runner=runner, checkpoint_store=store)
    output = asyncio.run(pipeline.run_resume("我从北京出发", PLAN_INTENTION, {}))

    assert output.paused is False
    assert output.answer_document is not None
    assert [section.kind for section in output.answer_document.sections] == ["trip"]
    # 重跑 intake 的结果出现在下游 previous_results（事实不丢失）
    assert "event_collection" in seen_previous["rag"]
    assert asyncio.run(store.get()) is None  # 完成即清除


def test_run_resume_without_checkpoint_delegates_to_run(tmp_path):
    store = _store(tmp_path)
    executed = []

    async def runner(**kwargs):
        executed.append(kwargs["agent_name"])
        if kwargs["agent_name"] == "rag_knowledge":
            return {"status": "success", "data": {"answer": "标准。"}}
        return {"status": "success", "data": {"results": {"summary": "晴"}}, "query_success": True}

    pipeline = MultiIntentPipeline(model=None, composer_model=None, agent_runner=runner, checkpoint_store=store)
    intention = {
        "intents": [
            {"type": "rag_knowledge", "confidence": 0.9, "should_call_skill": True},
            {"type": "information_query", "confidence": 0.9, "should_call_skill": True},
        ],
        "key_entities": {"destination": "南京"},
        "rewritten_query": "查南京差旅标准和天气",
    }
    output = asyncio.run(pipeline.run_resume("查南京差旅标准和天气", intention, {}))

    assert output.paused is False
    assert output.answer_document is not None
    assert executed == ["rag_knowledge", "information_query"]


def test_checkpoint_store_roundtrip_through_file(tmp_path):
    store = _store(tmp_path)
    asyncio.run(store.save(Checkpoint(
        user_id="u1", skill="plan-trip", request_id="req-x",
        pause_agent="event_collection", pause_field="planning_ready",
        steps_remaining=[{"agent_name": "rag_knowledge", "priority": 2}],
        collected_facts={"origin": "北京"},
        entities={"destination": "上海"},
    )))

    checkpoint = asyncio.run(store.get())
    assert checkpoint.skill == "plan-trip"
    assert checkpoint.collected_facts["origin"] == "北京"
    assert checkpoint.steps_remaining[0]["agent_name"] == "rag_knowledge"

    asyncio.run(store.clear())
    assert asyncio.run(store.get()) is None
