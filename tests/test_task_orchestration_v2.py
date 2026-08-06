import asyncio
import json
import time

import pytest

from core.orchestration.composer import AnswerComposer
from core.orchestration.decomposer import TaskDecomposer
from core.orchestration.executor import TaskExecutor
from core.orchestration.graph_builder import TaskGraphBuilder
from core.orchestration.models import IntentTask, TaskResult
from core.orchestration.pipeline import MultiIntentPipeline
from core.orchestration.validator import TaskValidator, supports_phase_one


QUERY = "我现在需要去南京出差，相关的差旅标准有什么，顺便给我查一下这两天南京的天气"
INTENTION = {
    "reasoning": "政策和天气是两个独立任务",
    "intents": [
        {"type": "rag_knowledge", "confidence": 0.88, "should_call_skill": True},
        {"type": "information_query", "confidence": 0.90, "should_call_skill": True},
    ],
    "key_entities": {"destination": "南京", "date": "这两天"},
    "rewritten_query": QUERY,
}


def test_phase_one_fallback_creates_scoped_queries():
    raw = TaskDecomposer.fallback(QUERY, ["rag_knowledge", "information_query"])
    tasks = TaskValidator().validate(raw, INTENTION)

    assert supports_phase_one(INTENTION) is True
    assert [task.intent for task in tasks] == ["rag_knowledge", "information_query"]
    assert "南京" in tasks[0].query
    assert "天气" not in tasks[0].query
    assert not any(term in tasks[0].query for term in ("住宿", "交通", "补贴", "报销", "审批"))
    assert "天气" in tasks[1].query
    assert not any(term in tasks[1].query for term in ("标准", "补贴", "报销"))


def test_task_validator_rejects_cross_intent_query_scope():
    raw = TaskDecomposer.fallback(QUERY, ["rag_knowledge", "information_query"])
    raw[0]["query"] += "并查询天气"

    with pytest.raises(ValueError, match="crossed into weather"):
        TaskValidator().validate(raw, INTENTION)


def test_task_validator_rejects_policy_scope_expansion():
    raw = TaskDecomposer.fallback(QUERY, ["rag_knowledge", "information_query"])
    raw[0]["query"] += "，包括住宿、交通和补贴"

    with pytest.raises(ValueError, match="expanded the user's scope"):
        TaskValidator().validate(raw, INTENTION)


def test_executor_runs_independent_tasks_concurrently():
    tasks = TaskValidator().validate(
        TaskDecomposer.fallback(QUERY, ["rag_knowledge", "information_query"]),
        INTENTION,
    )
    execution_tasks = TaskGraphBuilder().compile(tasks)
    started = 0
    both_started = asyncio.Event()

    async def runner(**kwargs):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.3)
        return {"status": "success", "data": {"query": kwargs["context"]["rewritten_query"]}}

    results = asyncio.run(TaskExecutor(runner).execute(execution_tasks, {}))

    assert [result.status for result in results] == ["success", "success"]
    assert results[0].data["query"] != results[1].data["query"]


def test_pipeline_keeps_agent_queries_scoped_and_builds_answer_document():
    seen = {}
    events = []

    async def runner(**kwargs):
        seen[kwargs["agent_name"]] = kwargs["context"]["rewritten_query"]
        if kwargs["agent_name"] == "rag_knowledge":
            return {
                "status": "success",
                "data": {
                    "answer": "南京住宿标准不超过400元/晚，国内出差无补贴。",
                    "sources": [{"file": "差旅规定", "section": "住宿标准"}],
                },
            }
        return {
            "status": "success",
            "data": {
                "results": {
                    "summary": "南京当前天气：晴，气温31°C，湿度73%。未来几日：2026-08-03: 晴，26~36°C；2026-08-04: 晴，25~33°C",
                    "sources": [{"title": "Open-Meteo", "url": "https://open-meteo.com"}],
                }
            },
        }

    async def progress(event):
        events.append(event.message_key)

    pipeline = MultiIntentPipeline(model=None, composer_model=None, agent_runner=runner)
    output = asyncio.run(pipeline.run(QUERY, INTENTION, {}, progress))

    assert "天气" not in seen["rag_knowledge"]
    assert "天气" in seen["information_query"]
    assert "标准" not in seen["information_query"]
    assert [section.kind for section in output.answer_document.sections] == ["policy", "weather"]
    assert len(output.answer_document.sections[1].days) == 2
    assert "400元/晚" in output.answer_document.plain_text
    assert "policy_searching" in events
    assert "travel_info_searching" in events
    assert events[-1] == "answer_ready"


def test_pipeline_returns_successful_policy_when_weather_fails():
    async def runner(**kwargs):
        if kwargs["agent_name"] == "rag_knowledge":
            return {"status": "success", "data": {"answer": "南京住宿不超过400元/晚。"}}
        return {
            "status": "error",
            "data": {},
            "error_code": "WEATHER_UNAVAILABLE",
            "error_message": "天气服务暂时不可用",
        }

    pipeline = MultiIntentPipeline(model=None, composer_model=None, agent_runner=runner)
    output = asyncio.run(pipeline.run(QUERY, INTENTION, {}))

    assert output.results[0].status == "success"
    assert output.results[1].status == "error"
    assert output.answer_document.sections[0].status == "success"
    assert output.answer_document.sections[1].status == "error"
    assert "南京住宿不超过400元/晚" in output.answer_document.plain_text
    assert "天气服务暂时不可用" in output.answer_document.plain_text


def test_llm_composer_returns_grounded_card_and_system_owned_sources():
    async def model(_messages):
        return {
            "content": json.dumps({
                "version": "1.0",
                "title": "南京差旅信息",
                "summary": "住宿标准与未来天气已整理。",
                "sections": [
                    {
                        "kind": "policy",
                        "title": "差旅标准",
                        "status": "success",
                        "body": "",
                        "items": [{"label": "住宿", "value": "不超过400元/晚", "detail": ""}],
                        "days": [],
                    },
                    {
                        "kind": "weather",
                        "title": "南京天气",
                        "status": "success",
                        "body": "",
                        "items": [],
                        "days": [{"date": "2026-08-03", "condition": "晴", "low": "26°C", "high": "36°C", "precipitation": None}],
                    },
                ],
                "notices": [],
            }, ensure_ascii=False)
        }

    tasks = [
        IntentTask.model_validate(item)
        for item in TaskDecomposer.fallback(QUERY, ["rag_knowledge", "information_query"])
    ]
    results = [
        TaskResult(
            task_id="policy", intent="rag_knowledge", agent_name="rag_knowledge", status="success",
            data={"answer": "南京住宿标准不超过400元/晚。"},
            evidence=[{"file": "差旅规定"}],
        ),
        TaskResult(
            task_id="weather", intent="information_query", agent_name="information_query", status="success",
            data={"results": {"summary": "2026-08-03: 晴，26~36°C"}},
            evidence=[{"title": "Open-Meteo", "url": "https://open-meteo.com"}],
            display_order=1,
        ),
    ]

    document = asyncio.run(AnswerComposer(model).compose(QUERY, tasks, results))

    assert document.sections[0].items[0].value == "不超过400元/晚"
    assert document.sections[1].days[0].precipitation == ""
    assert [source.title for source in document.sources] == ["差旅规定", "Open-Meteo"]
    assert "查看" not in document.plain_text
