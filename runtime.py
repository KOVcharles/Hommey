"""Internal backend factory for the Docker-hosted FastAPI application."""
from dataclasses import dataclass
from typing import Dict, Optional

from agents.intention_agent import IntentionAgent
from agents.lazy_agent_registry import LazyAgentRegistry
from agents.orchestration_agent import OrchestrationAgent
from settings import LLM_CONFIG, RESILIENCE_CONFIG, SYSTEM_CONFIG
from config_agentscope import init_agentscope
from context.memory_manager import MemoryManager
from utils.circuit_breaker import CircuitBreaker
from core.skill_store import SkillPlatformStore
from core.execution_budget import BudgetedModel


@dataclass
class AgentRuntime:
    model: object
    memory_manager: MemoryManager
    intention_agent: IntentionAgent
    agent_registry: LazyAgentRegistry
    orchestrator: OrchestrationAgent
    agent_cache: Dict
    attachment_service: Optional[object] = None  # 多模态附件服务（共享单例）


# 多模态附件服务单例：跨用户共享（按 user_id/attachment_id 隔离），惰性构造。
_shared_attachment_service = None


def get_shared_attachment_service():
    global _shared_attachment_service
    if _shared_attachment_service is None:
        from multimodal.service import AttachmentService

        _shared_attachment_service = AttachmentService()
    return _shared_attachment_service


def create_agent_runtime(
    user_id: str,
    session_id: str,
    agent_cache: Optional[Dict] = None,
    mcp_manager=None,
) -> AgentRuntime:
    """Create the agent runtime used by the FastAPI backend."""
    init_agentscope()

    from agentscope.model import OpenAIChatModel

    timeout_sec = SYSTEM_CONFIG.get("timeout", 60)
    raw_model = OpenAIChatModel(
        model_name=LLM_CONFIG["model_name"],
        api_key=LLM_CONFIG["api_key"],
        client_kwargs={
            "base_url": LLM_CONFIG["base_url"],
            "timeout": float(timeout_sec),
        },
        generate_kwargs={
            "temperature": LLM_CONFIG.get("temperature", 0.7),
            "max_tokens": LLM_CONFIG.get("max_tokens", 8192),
        },
    )
    model = BudgetedModel(raw_model)

    memory_manager = MemoryManager(
        user_id=user_id,
        session_id=session_id,
        llm_model=model,
    )

    intention_agent = IntentionAgent(
        name="IntentionAgent",
        model=model,
    )

    cache = agent_cache if agent_cache is not None else {}
    agent_registry = LazyAgentRegistry(
        model=model,
        cache=cache,
        memory_manager=memory_manager,
        mcp_manager=mcp_manager,
    )

    orchestrator = OrchestrationAgent(
        name="OrchestrationAgent",
        agent_registry=agent_registry,
        memory_manager=memory_manager,
        skill_store=SkillPlatformStore(),
    )

    return AgentRuntime(
        model=model,
        memory_manager=memory_manager,
        intention_agent=intention_agent,
        agent_registry=agent_registry,
        orchestrator=orchestrator,
        agent_cache=cache,
        attachment_service=get_shared_attachment_service(),
    )


def create_circuit_breaker() -> CircuitBreaker:
    """Create the shared circuit breaker from resilience config."""
    rc = RESILIENCE_CONFIG
    return CircuitBreaker(
        failure_threshold=rc.get("circuit_failure_threshold", 5),
        recovery_timeout_sec=rc.get("circuit_recovery_timeout_sec", 60.0),
        half_open_successes=rc.get("circuit_half_open_successes", 2),
    )
