"""Internal backend factory for the Docker-hosted FastAPI application."""
from dataclasses import dataclass
from typing import Optional

from settings import LLM_CONFIG, SYSTEM_CONFIG, SUPERVISOR_CONFIG
from config_agentscope import init_agentscope
from context.memory_manager import MemoryManager
from utils.circuit_breaker import CircuitBreaker
from core.execution_budget import BudgetedModel
from agent_runtime.engine import Supervisor
from agent_runtime.services import BusinessServices
from agent_runtime.store import RunStore


@dataclass
class AgentRuntime:
    model: object
    memory_manager: MemoryManager
    supervisor: Supervisor
    attachment_service: Optional[object] = None  # 多模态附件服务（共享单例）


# 多模态附件服务单例：跨用户共享（按 user_id/attachment_id 隔离），惰性构造。
_shared_attachment_service = None


def _uses_model_studio_deepseek_v4(config: dict) -> bool:
    """Whether this OpenAI-compatible endpoint supports enable_thinking."""
    model_name = str(config.get("model_name") or "").lower()
    base_url = str(config.get("base_url") or "").lower()
    return model_name.startswith("deepseek-v4-") and "aliyuncs.com" in base_url


def _generate_kwargs(config: dict) -> dict:
    """Build provider-safe OpenAI generation options for one configured model."""
    kwargs = {
        "temperature": config.get("temperature", 0.7),
        "max_tokens": config.get("max_tokens", 8192),
    }
    # Model Studio exposes this as a non-standard OpenAI parameter, so the
    # OpenAI Python client must carry it inside extra_body.
    if _uses_model_studio_deepseek_v4(config):
        kwargs["extra_body"] = {
            "enable_thinking": bool(config.get("enable_thinking", False)),
        }
    return kwargs


def get_shared_attachment_service():
    global _shared_attachment_service
    if _shared_attachment_service is None:
        from multimodal.service import AttachmentService

        _shared_attachment_service = AttachmentService()
    return _shared_attachment_service


def create_agent_runtime(
    user_id: str,
    session_id: str,
) -> AgentRuntime:
    """Create the agent runtime used by the FastAPI backend."""
    init_agentscope()

    from agentscope.model import OpenAIChatModel

    timeout_sec = SYSTEM_CONFIG.get("timeout", 60)
    def create_model(config):
        raw = OpenAIChatModel(
            model_name=config["model_name"],
            api_key=config["api_key"],
            client_kwargs={
                "base_url": config["base_url"],
                "timeout": float(timeout_sec),
            },
            generate_kwargs=_generate_kwargs(config),
        )
        return BudgetedModel(raw)

    model = create_model(LLM_CONFIG)

    memory_manager = MemoryManager(
        user_id=user_id,
        session_id=session_id,
    )

    pool = getattr(memory_manager.long_term, "pool", None)
    if pool is None:
        raise ValueError("The travel agent requires PostgreSQL memory")
    supervisor = Supervisor(model, BusinessServices(memory_manager), RunStore(pool), SUPERVISOR_CONFIG)

    return AgentRuntime(
        model=model,
        memory_manager=memory_manager,
        attachment_service=get_shared_attachment_service(),
        supervisor=supervisor,
    )


def create_circuit_breaker() -> CircuitBreaker:
    """Create the process-shared Redis-backed circuit breaker.

    Returns the async-native ``utils.redis_coordination.RedisCircuitBreaker``
    (duck-typed against the legacy sync ``CircuitBreaker`` interface); callers
    must ``await`` the async methods. The legacy synchronous class is kept for
    remaining sync call paths.
    """
    from utils.redis_coordination import create_redis_circuit_breaker

    return create_redis_circuit_breaker()
