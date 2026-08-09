from pathlib import Path
import importlib.util

from agents.lazy_agent_registry import LazyAgentRegistry
from agentscope.message import Msg


def test_rag_knowledge_skill_is_registered():
    registry = LazyAgentRegistry(model=None, cache={})

    assert "rag_knowledge" in registry
    assert "ask-question" in registry.keys()


def test_rag_knowledge_skill_has_agent_script():
    script_path = Path(".agents/skills/ask-question/script/agent.py")

    assert script_path.exists()


def test_rag_agent_prefers_active_task_query_over_request_wide_query():
    import json

    script_path = Path(".agents/skills/ask-question/script/agent.py")
    spec = importlib.util.spec_from_file_location("rag_agent_test_module_scope", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent = object.__new__(module.RAGKnowledgeAgent)
    message = Msg(
        name="Orchestrator",
        role="user",
        content=json.dumps({
            "context": {
                "agent_query": "查南京天气，然后查询差旅标准并规划行程",
                "active_task": {
                    "query": "南京 查询公司的差旅标准",
                },
            },
        }, ensure_ascii=False),
    )

    query = agent._extract_query(message)

    assert query == "南京 查询公司的差旅标准"
    assert "天气" not in query and "规划" not in query


def test_rag_agent_expands_broad_standard_retrieval_without_weather_terms():
    script_path = Path(".agents/skills/ask-question/script/agent.py")
    spec = importlib.util.spec_from_file_location("rag_agent_test_module_multi_query", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeRetriever:
        top_k = 3

        def __init__(self):
            self.queries = []

        def search(self, query, top_k=None):
            self.queries.append(query)
            return [{"id": query, "content": query}]

    agent = object.__new__(module.RAGKnowledgeAgent)
    agent.retriever = FakeRetriever()

    documents = agent._retrieve_for_question(
        "南京 查询公司的差旅标准"
    )

    assert agent.retriever.queries[0] == "南京 差旅标准"
    assert any("南京 出差 住宿标准" == query for query in agent.retriever.queries)
    assert any("南京 出差 交通标准" == query for query in agent.retriever.queries)
    assert any("南京 出差 餐饮补贴 报销标准" == query for query in agent.retriever.queries)
    assert all("天气" not in query and "规划" not in query for query in agent.retriever.queries)
    assert len(documents) == 4


def test_rag_standard_expansion_is_destination_driven_not_domestic_hardcoded():
    script_path = Path(".agents/skills/ask-question/script/agent.py")
    spec = importlib.util.spec_from_file_location("rag_agent_test_module_tokyo", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.RAGKnowledgeAgent._leading_location("东京的差旅标准") == "东京"
    assert module.RAGKnowledgeAgent._leading_location("东京 差旅标准") == "东京"
    assert module.RAGKnowledgeAgent._leading_location("Tokyo 差旅标准") == "Tokyo"

    class FakeRetriever:
        top_k = 3

        def __init__(self):
            self.queries = []

        def search(self, query, top_k=None):
            self.queries.append(query)
            return []

    agent = object.__new__(module.RAGKnowledgeAgent)
    agent.retriever = FakeRetriever()
    agent._retrieve_for_question("查询东京的差旅标准")

    assert "东京 出差 住宿标准" in agent.retriever.queries
    assert "东京 出差 交通标准" in agent.retriever.queries
    assert all("国内出差" not in query and "南京" not in query for query in agent.retriever.queries)


def test_rag_agent_extracts_async_stream_text():
    script_path = Path(".agents/skills/ask-question/script/agent.py")
    spec = importlib.util.spec_from_file_location("rag_agent_test_module", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    agent = object.__new__(module.RAGKnowledgeAgent)

    async def stream():
        yield "餐费"
        yield {"content": "可以报销"}

    import asyncio

    text = asyncio.run(agent._extract_model_text(stream()))

    assert text == "餐费可以报销"


def test_rag_agent_collapses_cumulative_stream_text():
    script_path = Path(".agents/skills/ask-question/script/agent.py")
    spec = importlib.util.spec_from_file_location("rag_agent_test_module_cumulative", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    agent = object.__new__(module.RAGKnowledgeAgent)

    async def stream():
        yield {"content": "根据知识库信息，酒店"}
        yield {"content": "根据知识库信息，酒店价格超过标准时，可以说明情况。"}
        yield {"content": "根据知识库信息，酒店价格超过标准时，可以说明情况。超出部分自理。"}

    import asyncio

    text = asyncio.run(agent._extract_model_text(stream()))

    assert text == "根据知识库信息，酒店价格超过标准时，可以说明情况。超出部分自理。"


def test_rag_agent_preserves_streaming_for_answer_generation():
    script_path = Path(".agents/skills/ask-question/script/agent.py")
    spec = importlib.util.spec_from_file_location("rag_agent_test_module_stream", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeModel:
        def __init__(self):
            self.stream = True
            self.seen_stream = None

        async def __call__(self, messages):
            self.seen_stream = self.stream

            async def stream():
                yield {"choices": [{"delta": {"content": "餐费"}}]}
                yield {"choices": [{"delta": {"content": "可以按标准报销"}}]}

            return stream()

    agent = object.__new__(module.RAGKnowledgeAgent)
    agent.model = FakeModel()
    agent.skill_loader = type("SkillLoader", (), {"get_skill_content": lambda self, name: ""})()

    import asyncio

    answer = asyncio.run(agent._generate_answer("餐费可以补吗", "餐费标准"))

    assert answer == "餐费可以按标准报销"
    assert agent.model.seen_stream is True
    assert agent.model.stream is True


def test_rag_agent_llm_failure_does_not_dump_raw_context():
    script_path = Path(".agents/skills/ask-question/script/agent.py")
    spec = importlib.util.spec_from_file_location("rag_agent_test_module_failure", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FailingModel:
        async def __call__(self, messages):
            raise KeyError("text")

    agent = object.__new__(module.RAGKnowledgeAgent)
    agent.model = FailingModel()
    agent.skill_loader = type("SkillLoader", (), {"get_skill_content": lambda self, name: ""})()

    import asyncio

    answer = asyncio.run(agent._generate_answer("酒店价格超过标准怎么办？", "Q9: 酒店价格超过标准怎么办？\nA9: 很长的原文"))

    assert "很长的原文" not in answer
    assert "生成面向用户的总结回答时出错" in answer


def test_rag_agent_normalizes_contradictory_related_policy_answer():
    script_path = Path(".agents/skills/ask-question/script/agent.py")
    spec = importlib.util.spec_from_file_location("rag_agent_test_module_normalize", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    agent = object.__new__(module.RAGKnowledgeAgent)
    answer = (
        "抱歉，知识库中没有找到关于固定补贴的相关信息。"
        "知识库中仅规定了费用报销标准：需提供发票并按标准报销。"
    )

    normalized = agent._normalize_answer(answer)

    assert "没有找到关于固定补贴的相关信息" not in normalized
    assert normalized.startswith("知识库没有明确规定用户问题中的具体说法")
    assert "需提供发票并按标准报销" in normalized


def test_rag_agent_keeps_true_no_knowledge_answer_unchanged():
    script_path = Path(".agents/skills/ask-question/script/agent.py")
    spec = importlib.util.spec_from_file_location("rag_agent_test_module_true_empty", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    agent = object.__new__(module.RAGKnowledgeAgent)
    answer = "抱歉，知识库中没有找到相关信息。"

    assert agent._normalize_answer(answer) == answer


def test_rag_agent_normalizes_partial_policy_answer_without_domain_special_case():
    script_path = Path(".agents/skills/ask-question/script/agent.py")
    spec = importlib.util.spec_from_file_location("rag_agent_test_module_partial", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    agent = object.__new__(module.RAGKnowledgeAgent)
    answer = "未提及是否可以直接操作，但相关流程要求先提交申请并经主管审批。"

    normalized = agent._normalize_answer(answer)

    assert "未提及是否可以直接操作" not in normalized
    assert "提交申请并经主管审批" in normalized


def test_rag_agent_async_iterable_check_ignores_key_error():
    script_path = Path(".agents/skills/ask-question/script/agent.py")
    spec = importlib.util.spec_from_file_location("rag_agent_test_module_iterable", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class WeirdResponse:
        def __getattr__(self, name):
            raise KeyError(name)

    agent = object.__new__(module.RAGKnowledgeAgent)

    assert agent._is_async_iterable(WeirdResponse()) is False


def test_rag_agent_extracts_choice_content_when_text_attr_raises_key_error():
    script_path = Path(".agents/skills/ask-question/script/agent.py")
    spec = importlib.util.spec_from_file_location("rag_agent_test_module_choice", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class ChoiceLikeResponse:
        def __getattr__(self, name):
            if name == "text":
                raise KeyError("text")
            if name == "content":
                return [{"type": "text", "text": "餐费可以按标准报销"}]
            raise AttributeError(name)

    agent = object.__new__(module.RAGKnowledgeAgent)

    assert agent._extract_chunk_text(ChoiceLikeResponse()) == "餐费可以按标准报销"
