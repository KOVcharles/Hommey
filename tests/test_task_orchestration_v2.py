import asyncio
import json

import pytest

from core.orchestration.composer import AnswerComposer
from core.orchestration.decomposer import TaskDecomposer
from core.orchestration.executor import TaskExecutor
from core.orchestration.graph_builder import TaskGraphBuilder
from core.orchestration.models import IntentTask, TaskResult
from core.orchestration.pipeline import MultiIntentPipeline
from core.orchestration.validator import TaskValidator, supports_task_pipeline


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
    raw = TaskDecomposer.fallback(
        QUERY, ["rag_knowledge", "information_query"], INTENTION["key_entities"]
    )
    tasks = TaskValidator().validate(raw, INTENTION)

    assert supports_task_pipeline(INTENTION) is True
    assert [task.intent for task in tasks] == ["rag_knowledge", "information_query"]
    assert "南京" in tasks[0].query
    assert "天气" not in tasks[0].query
    assert not any(term in tasks[0].query for term in ("住宿", "交通", "补贴", "报销", "审批"))
    assert "天气" in tasks[1].query
    assert not any(term in tasks[1].query for term in ("标准", "补贴", "报销"))


def test_task_validator_rejects_cross_intent_query_scope():
    raw = TaskDecomposer.fallback(
        QUERY, ["rag_knowledge", "information_query"], INTENTION["key_entities"]
    )
    raw[0]["query"] += "并查询天气"

    with pytest.raises(ValueError, match="forbidden scope"):
        TaskValidator().validate(raw, INTENTION)


def test_validator_restores_declared_query_anchors_dropped_by_llm():
    raw = [
        {
            "task_id": "policy_query", "intent": "rag_knowledge",
            "query": "查询公司的差旅标准", "entities": {}, "depends_on": [],
            "side_effect": False, "failure_policy": "continue", "display_order": 0,
        },
        {
            "task_id": "weather_query", "intent": "information_query",
            "query": "查询最近天气", "entities": {}, "depends_on": [],
            "side_effect": False, "failure_policy": "continue", "display_order": 1,
        },
    ]

    tasks = TaskValidator().validate(raw, INTENTION)
    policy, weather = tasks

    assert policy.entities["destination"] == "南京"
    assert policy.query.startswith("南京 ")
    assert "天气" not in policy.query
    assert weather.entities["start_date"] == "这两天"
    assert weather.query.startswith("南京 这两天 ")
    assert "标准" not in weather.query


@pytest.mark.parametrize(("destination", "date"), [
    ("南京", "2026-08-10"),
    ("东京", "2026-09-01"),
    ("上海", "明天"),
    ("成都", "下周一"),
])
def test_query_scope_and_entity_anchors_are_city_agnostic(destination, date):
    intention = {
        "intents": [
            {"type": "rag_knowledge", "confidence": .9, "should_call_skill": True},
            {"type": "information_query", "confidence": .9, "should_call_skill": True},
        ],
        "key_entities": {"destination": destination, "date": date},
        "rewritten_query": f"去{destination}出差，查询天气和差旅标准",
    }
    raw = [
        {
            "task_id": "policy", "intent": "rag_knowledge",
            "query": "查询差旅标准", "entities": {}, "depends_on": [],
            "side_effect": False, "failure_policy": "continue", "display_order": 0,
        },
        {
            "task_id": "weather", "intent": "information_query",
            "query": "查询天气", "entities": {}, "depends_on": [],
            "side_effect": False, "failure_policy": "continue", "display_order": 1,
        },
    ]

    policy, weather = TaskValidator().validate(raw, intention)

    assert destination in policy.query and "天气" not in policy.query
    assert destination in weather.query and str(date) in weather.query
    assert "标准" not in weather.query


def test_task_validator_rejects_policy_scope_expansion():
    raw = TaskDecomposer.fallback(
        QUERY, ["rag_knowledge", "information_query"], INTENTION["key_entities"]
    )
    raw[0]["query"] += "，包括住宿、交通和补贴"

    with pytest.raises(ValueError, match="expanded the user's scope"):
        TaskValidator().validate(raw, INTENTION)


def test_task_validator_merges_duplicate_intent_nodes():
    raw = TaskDecomposer.fallback(
        QUERY, ["rag_knowledge", "information_query"], INTENTION["key_entities"]
    )
    raw.append(dict(raw[0], task_id="policy-extra", query=raw[0]["query"] + "（补充）"))

    tasks = TaskValidator().validate(raw, INTENTION)
    assert len(tasks) == 2  # 意图节点级去重：rag_knowledge 合并
    assert [task.intent for task in tasks] == ["rag_knowledge", "information_query"]
    rag = next(task for task in tasks if task.intent == "rag_knowledge")
    assert "补充" in rag.query


def test_task_validator_rejects_unauthorized_intent():
    raw = TaskDecomposer.fallback(
        QUERY, ["rag_knowledge", "information_query"], INTENTION["key_entities"]
    )
    raw.append({
        "task_id": "rogue",
        "intent": "memory_query",
        "query": "查询我的出差历史",
        "depends_on": [],
        "side_effect": False,
        "failure_policy": "continue",
        "display_order": 2,
    })
    with pytest.raises(ValueError, match="not authorized"):
        TaskValidator().validate(raw, INTENTION)


def test_task_validator_rejects_undeclared_side_effect():
    raw = TaskDecomposer.fallback(
        QUERY, ["rag_knowledge", "information_query"], INTENTION["key_entities"]
    )
    raw[0]["side_effect"] = True  # rag_knowledge 未声明 side_effect_allowed

    with pytest.raises(ValueError, match="side effect not allowed"):
        TaskValidator().validate(raw, INTENTION)


def test_task_validator_rejects_unknown_dependency_target():
    raw = TaskDecomposer.fallback(
        QUERY, ["rag_knowledge", "information_query"], INTENTION["key_entities"]
    )
    raw[1]["depends_on"] = ["no_such_intent"]

    with pytest.raises(ValueError, match="unknown dependency target"):
        TaskValidator().validate(raw, INTENTION)


def test_task_validator_rejects_dependency_cycle():
    raw = TaskDecomposer.fallback(
        QUERY, ["rag_knowledge", "information_query"], INTENTION["key_entities"]
    )
    raw[0]["depends_on"] = ["information_query"]
    raw[1]["depends_on"] = ["rag_knowledge"]

    with pytest.raises(ValueError, match="cycle"):
        TaskValidator().validate(raw, INTENTION)


def test_executor_runs_independent_tasks_concurrently():
    tasks = TaskValidator().validate(
        TaskDecomposer.fallback(QUERY, ["rag_knowledge", "information_query"], INTENTION["key_entities"]),
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

    results, pauses = asyncio.run(TaskExecutor(runner).execute(execution_tasks, {}))

    assert pauses == []
    assert [result.status for result in results] == ["success", "success"]
    assert results[0].data["query"] != results[1].data["query"]


def test_plan_trip_expands_to_six_step_dag():
    plan_query = "我明天去上海出差3天，帮我规划一下行程"
    plan_intention = {
        "intents": [{"type": "itinerary_planning", "confidence": 0.9, "should_call_skill": True}],
        "key_entities": {"destination": "上海", "date": "明天", "duration": "3天"},
        "rewritten_query": plan_query,
    }
    raw = TaskDecomposer.fallback(
        plan_query, ["itinerary_planning"], plan_intention["key_entities"]
    )
    tasks = TaskValidator().validate(raw, plan_intention)
    execution_tasks = TaskGraphBuilder().compile(tasks)

    assert [task.agent_name for task in execution_tasks] == [
        "event_collection", "rag_knowledge", "information_query", "train_query",
        "itinerary_planning", "trip_compliance",
    ]
    assert [task.priority for task in execution_tasks] == [1, 2, 2, 2, 3, 4]
    assert [task.failure_policy for task in execution_tasks] == [
        "abort", "abort", "continue", "continue", "abort", "continue",
    ]
    batches = TaskGraphBuilder.batches(execution_tasks)
    assert [[task.agent_name for task in batch] for batch in batches] == [
        ["event_collection"],
        ["rag_knowledge", "information_query", "train_query"],
        ["itinerary_planning"],
        ["trip_compliance"],
    ]
    # 每步 query 都从模板渲染，不污染其他意图词域
    for task in execution_tasks:
        assert task.query, task.agent_name


def test_plan_trip_absorbs_overlapping_independent_intents():
    plan_query = "我明天去上海出差3天，帮我规划一下行程，顺便查下上海差旅标准和天气"
    plan_intention = {
        "intents": [
            {"type": "itinerary_planning", "confidence": 0.9, "should_call_skill": True},
            {"type": "rag_knowledge", "confidence": 0.85, "should_call_skill": True},
            {"type": "information_query", "confidence": 0.88, "should_call_skill": True},
        ],
        "key_entities": {"destination": "上海", "date": "明天", "duration": "3天", "purpose": "出差"},
        "rewritten_query": plan_query,
    }
    raw = TaskDecomposer.fallback(
        plan_query,
        ["itinerary_planning", "rag_knowledge", "information_query"],
        plan_intention["key_entities"],
    )
    tasks = TaskValidator().validate(raw, plan_intention)
    execution_tasks = TaskGraphBuilder().compile(tasks)

    # 独立天气 Goal 只复用 weather 能力；plan-trip 仍单独查询普通交通，
    # 避免用一个更窄的天气结果顶替整个天气+交通步骤。
    assert sorted(task.agent_name for task in execution_tasks) == sorted([
        "event_collection", "rag_knowledge", "information_query", "information_query",
        "train_query", "itinerary_planning", "trip_compliance",
    ])
    by_agent = {task.agent_name: task for task in execution_tasks}
    info_tasks = [
        task for task in execution_tasks if task.agent_name == "information_query"
    ]
    assert "标准" in by_agent["rag_knowledge"].query
    assert any("天气" in task.query for task in info_tasks)
    assert any(
        task.capabilities == ["local_transport"] and "天气" not in task.query
        for task in info_tasks
    )
    assert "天气" not in by_agent["rag_knowledge"].query
    # train-query 步骤带 scoped 车次 query，不污染其他意图词域。
    assert "车次" in by_agent["train_query"].query
    assert "标准" not in by_agent["train_query"].query


def test_abort_halt_skips_downstream_steps():
    plan_query = "我明天去上海出差3天，帮我规划一下行程"
    plan_intention = {
        "intents": [{"type": "itinerary_planning", "confidence": 0.9, "should_call_skill": True}],
        "key_entities": {"destination": "上海", "date": "明天", "duration": "3天"},
        "rewritten_query": plan_query,
    }

    async def runner(**kwargs):
        agent = kwargs["agent_name"]
        if agent == "event_collection":
            return {"status": "success", "data": {
                "planning_ready": True, "origin": "北京", "destination": "上海",
                "start_date": "2026-08-08", "duration_days": 3, "trip_purpose": "客户拜访",
            }}
        if agent == "rag_knowledge":
            return {"status": "success", "data": {"answer": "住宿标准400元。"}}
        if agent == "information_query":
            return {"status": "success", "data": {"results": {"summary": "晴"}}, "query_success": True}
        if agent == "train_query":
            return {"status": "success", "data": {"results": {"trains": []}}, "query_success": True}
        if agent == "itinerary_planning":
            return {"status": "error", "data": {}, "error_code": "PLAN_FAILED", "error_message": "无法生成行程"}
        if agent == "trip_compliance":
            raise AssertionError("trip_compliance 应在 abort 后跳过")

    pipeline = MultiIntentPipeline(model=None, composer_model=None, agent_runner=runner)
    output = asyncio.run(pipeline.run(plan_query, plan_intention, {}))

    by_agent = {result.agent_name: result for result in output.results}
    assert by_agent["itinerary_planning"].status == "error"
    assert by_agent["trip_compliance"].status == "skipped"
    assert by_agent["trip_compliance"].error_code == "UPSTREAM_DEPENDENCY_FAILED"
    assert [section.kind for section in output.answer_document.sections] == ["trip"]
    assert output.answer_document.sections[0].status == "error"


def test_same_batch_double_abort_skips_both_goals_downstreams():
    # P1-4：同一批内两个 goal 各自的 abort 步骤同时失败时，两侧下游都要标
    # SKIPPED。旧实现 break 只处理批内第一个 abort 失败，第二个 goal 的下游
    # 会带失败依赖继续执行（runner 断言兜底：任何本应跳过的步骤都不允许运行）。
    plan_query = "检查南京出差行程是否合规，并规划行程"
    intention = {
        "intents": [
            {"type": "itinerary_planning", "confidence": 0.9, "should_call_skill": True},
            {"type": "trip_compliance", "confidence": 0.85, "should_call_skill": True},
        ],
        "key_entities": {"destination": "南京"},
        "rewritten_query": plan_query,
    }

    async def runner(**kwargs):
        agent = kwargs["agent_name"]
        task_id = kwargs["task_params"]["task_id"]
        if agent == "event_collection":
            return {"status": "error", "data": {}, "error_code": "INTAKE_FAILED", "error_message": "采集失败"}
        if agent == "rag_knowledge" and task_id.startswith("trip_compliance-"):
            return {"status": "error", "data": {}, "error_code": "RAG_FAILED", "error_message": "政策查询失败"}
        raise AssertionError(f"abort 后不应执行 {task_id}")

    pipeline = MultiIntentPipeline(model=None, composer_model=None, agent_runner=runner)
    output = asyncio.run(pipeline.run(plan_query, intention, {}))

    by_id = {result.task_id: result for result in output.results}
    # 两个 goal 各自的 abort 失败
    assert by_id["itinerary_planning-event_collection"].status == "error"
    assert by_id["trip_compliance-event_collection"].status == "error"
    # 两侧下游全部 SKIPPED
    assert by_id["itinerary_planning-itinerary_planning"].status == "skipped"
    assert by_id["itinerary_planning-itinerary_planning"].error_code == "UPSTREAM_DEPENDENCY_FAILED"
    assert by_id["itinerary_planning-trip_compliance"].status == "skipped"
    assert by_id["trip_compliance-trip_compliance"].status == "skipped"
    assert by_id["trip_compliance-trip_compliance"].error_code == "UPSTREAM_DEPENDENCY_FAILED"


def test_pause_gate_halts_workflow_and_builds_intake_presentation():
    plan_query = "我明天去上海出差3天，帮我规划一下行程"
    plan_intention = {
        "intents": [{"type": "itinerary_planning", "confidence": 0.9, "should_call_skill": True}],
        "key_entities": {"destination": "上海", "date": "明天", "duration": "3天"},
        "rewritten_query": plan_query,
    }

    async def runner(**kwargs):
        if kwargs["agent_name"] == "event_collection":
            return {"status": "success", "data": {
                "destination": "上海", "start_date": "2026-08-08",
                "duration_days": 3, "trip_purpose": "客户拜访",
                "planning_ready": False,  # 缺 origin
            }}
        raise AssertionError(f"不应执行 {kwargs['agent_name']}")

    pipeline = MultiIntentPipeline(model=None, composer_model=None, agent_runner=runner)
    output = asyncio.run(pipeline.run(plan_query, plan_intention, {}))

    assert output.paused is True
    assert output.pause_info.pause_agent == "event_collection"
    assert [step["agent_name"] for step in output.pause_info.steps_remaining] == [
        "rag_knowledge", "information_query", "train_query",
        "itinerary_planning", "trip_compliance",
    ]
    assert output.presentation_document.type == "trip_intake"
    assert output.presentation_document.status == "collecting_required"
    assert [prompt.key for prompt in output.presentation_document.missing_required] == ["origin"]
    assert output.answer_document is None


def test_pipeline_keeps_agent_queries_scoped_and_builds_answer_document():
    seen = {}
    events = []

    async def runner(**kwargs):
        context = kwargs["context"]
        seen[kwargs["agent_name"]] = {
            key: context[key]
            for key in ("original_query", "agent_query", "rewritten_query", "user_query")
        }
        assert set(seen[kwargs["agent_name"]].values()) == {
            kwargs["context"]["active_task"]["query"]
        }
        assert context["request_original_query"] == QUERY
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
                },
                "query_success": True,
            },
        }

    async def progress(event):
        events.append(event.message_key)

    pipeline = MultiIntentPipeline(model=None, composer_model=None, agent_runner=runner)
    output = asyncio.run(pipeline.run(QUERY, INTENTION, {}, progress))

    assert "天气" not in seen["rag_knowledge"]["agent_query"]
    assert "天气" in seen["information_query"]["agent_query"]
    assert "标准" not in seen["information_query"]["agent_query"]
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


def test_result_rules_convert_query_failure_to_error():
    # information_query 的 result_rules：query_success=False → 归一化为 error
    async def runner(**kwargs):
        return {"status": "success", "data": {"query_success": False, "results": {"message": "外部服务超时"}}}

    pipeline = MultiIntentPipeline(model=None, composer_model=None, agent_runner=runner)
    intention = {
        "intents": [{"type": "information_query", "confidence": 0.9, "should_call_skill": True}],
        "key_entities": {"destination": "南京"},
        "rewritten_query": "查一下南京天气",
    }
    output = asyncio.run(pipeline.run("查一下南京天气", intention, {}))

    result = output.results[0]
    assert result.status == "error"
    assert result.error_code == "INFORMATION_QUERY_UNAVAILABLE"
    assert "外部服务超时" in output.answer_document.sections[0].body


def test_structured_results_bypass_second_llm_and_keep_system_owned_sources():
    called = False

    async def model(_messages):
        nonlocal called
        called = True
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
        for item in TaskDecomposer.fallback(QUERY, ["rag_knowledge", "information_query"], INTENTION["key_entities"])
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

    assert called is False
    assert document.sections[0].body == "南京住宿标准不超过400元/晚。"
    assert document.sections[1].days[0].precipitation == ""
    assert [source.title for source in document.sources] == ["差旅规定", "Open-Meteo"]
    assert "查看" not in document.plain_text


def test_declarative_fallback_covers_all_skill_intents():
    # 每个 skill-backed 意图都能产出可验证的语义任务，不依赖 LLM 分支。
    from core.intent_catalog import SKILL_INTENTS

    for intent in SKILL_INTENTS:
        intention = {
            "intents": [{"type": intent, "confidence": 0.9, "should_call_skill": True}],
            "key_entities": {"destination": "上海", "start_date": "明天", "duration": "3天"},
            "rewritten_query": f"帮我处理{intent}",
        }
        raw = TaskDecomposer.fallback(f"帮我处理{intent}", [intent], intention["key_entities"])
        tasks = TaskValidator().validate(raw, intention)
        assert [task.intent for task in tasks] == [intent]


def test_manager_raises_on_pipeline_abort_but_degrades_continue():
    # 阶段 4 gate 放开后 manager 用 abort 语义判定硬失败：continue 降级（如天气
    # 不可用）继续走卡片，abort 步骤的错误才上抛为公共错误。
    from types import SimpleNamespace

    from core.orchestration.models import ExecutionTask, TaskResult
    from webui_new.core.errors import UpstreamError
    from webui_new.manager import HommeyWebInstance

    instance = object.__new__(HommeyWebInstance)
    instance.user_id = "u1"

    continue_task = ExecutionTask(
        intent="information_query", task_id="information_query-information_query",
        query="查天气", agent_name="information_query", priority=1,
        failure_policy="continue", display_order=0,
    )
    continue_result = TaskResult(
        task_id="information_query-information_query", intent="information_query",
        agent_name="information_query", status="error",
        error_code="INFORMATION_QUERY_UNAVAILABLE", display_order=0,
    )
    abort_task = ExecutionTask(
        intent="itinerary_planning", task_id="itinerary_planning-itinerary_planning",
        query="规划行程", agent_name="itinerary_planning", priority=3,
        failure_policy="abort", display_order=1,
    )
    abort_result = TaskResult(
        task_id="itinerary_planning-itinerary_planning", intent="itinerary_planning",
        agent_name="itinerary_planning", status="error",
        error_code="AGENT_EXECUTION_FAILED", error_message="生成失败", display_order=1,
    )

    # 只有 continue 失败 → 不抛错，交给 composer 降级。
    instance._raise_on_pipeline_errors(SimpleNamespace(
        execution_tasks=[continue_task], results=[continue_result],
    ))

    # abort 失败（即便同时存在 continue 失败）→ 上抛。
    with pytest.raises(UpstreamError):
        instance._raise_on_pipeline_errors(SimpleNamespace(
            execution_tasks=[continue_task, abort_task],
            results=[continue_result, abort_result],
        ))
