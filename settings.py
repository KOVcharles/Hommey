"""
Runtime settings for the Hommey multi-agent system.

Do not put real API keys in this file. Set HOMMEY_API_KEY in your shell or
local .env file instead.
"""
import os

from dotenv import load_dotenv
load_dotenv()  # 加载项目根目录的 .env 文件


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    return float(value)


def _optional_env(name: str):
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    if not value or value.startswith("#"):
        return None
    return value


LLM_CONFIG = {
    "api_key": os.getenv("HOMMEY_API_KEY", ""),
    "model_name": os.getenv("HOMMEY_MODEL_NAME", "deepseek-v3"),
    "base_url": os.getenv(
        "HOMMEY_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    "temperature": _float_env("HOMMEY_TEMPERATURE", 0.7),
    "max_tokens": _int_env("HOMMEY_MAX_TOKENS", 8192),
}


COMPOSER_CONFIG = {
    "enabled": _bool_env("HOMMEY_COMPOSER_ENABLED", True),
    "api_key": os.getenv("HOMMEY_COMPOSER_API_KEY") or LLM_CONFIG["api_key"],
    "model_name": os.getenv("HOMMEY_COMPOSER_MODEL_NAME") or LLM_CONFIG["model_name"],
    "base_url": os.getenv("HOMMEY_COMPOSER_BASE_URL") or LLM_CONFIG["base_url"],
    "temperature": _float_env("HOMMEY_COMPOSER_TEMPERATURE", 0.2),
    "max_tokens": _int_env("HOMMEY_COMPOSER_MAX_TOKENS", 4096),
}


ORCHESTRATION_V2_CONFIG = {
    # Phase one is deliberately limited to independent policy + public-info requests.
    "enabled": _bool_env("HOMMEY_TASK_ORCHESTRATION_V2", True),
}


TRIP_INTAKE_CONFIG = {
    "enabled": _bool_env("HOMMEY_TRIP_INTAKE_CARD", True),
}


SYSTEM_CONFIG = {
    "enable_llm": _bool_env("HOMMEY_ENABLE_LLM", True),
    "log_level": os.getenv("HOMMEY_LOG_LEVEL", "INFO"),
    "log_format": os.getenv("HOMMEY_LOG_FORMAT", "text"),
    "preflight_include_network": _bool_env("HOMMEY_PREFLIGHT_INCLUDE_NETWORK", False),
    "max_retries": _int_env("HOMMEY_SYSTEM_MAX_RETRIES", 3),
    "timeout": _int_env("HOMMEY_TIMEOUT", 60),
}


RAG_CONFIG = {
    "embedding_backend": os.getenv("HOMMEY_RAG_EMBEDDING_BACKEND", "siliconflow").lower(),
    "embedding_model": os.getenv(
        "HOMMEY_EMBEDDING_MODEL",
        "BAAI/bge-m3",
    ),
    "embedding_api_key": _optional_env("HOMMEY_EMBEDDING_API_KEY")
    or _optional_env("SILICONFLOW_API_KEY"),
    "embedding_base_url": os.getenv(
        "HOMMEY_EMBEDDING_BASE_URL",
        "https://api.siliconflow.cn/v1",
    ),
    "embedding_dimension": _int_env("HOMMEY_EMBEDDING_DIMENSION", 1024),
    "embedding_batch_size": _int_env("HOMMEY_EMBEDDING_BATCH_SIZE", 32),
    "embedding_timeout_sec": _float_env("HOMMEY_EMBEDDING_TIMEOUT_SEC", 30.0),
    "documents_dir": os.getenv(
        "HOMMEY_RAG_DOCUMENTS_DIR",
        "data/documents",
    ),
    "knowledge_base_path": os.getenv(
        "HOMMEY_RAG_KNOWLEDGE_BASE_PATH",
        "data/rag_knowledge",
    ),
    "collection_name": os.getenv(
        "HOMMEY_RAG_COLLECTION",
        "business_travel_knowledge",
    ),
    "chunk_size": _int_env("HOMMEY_RAG_CHUNK_SIZE", 600),
    "chunk_overlap": _int_env("HOMMEY_RAG_CHUNK_OVERLAP", 100),
    "top_k": _int_env("HOMMEY_RAG_TOP_K", 3),
    "vector_top_k": _int_env("HOMMEY_RAG_VECTOR_TOP_K", 10),
    "bm25_top_k": _int_env("HOMMEY_RAG_BM25_TOP_K", 10),
}


SKILL_CONFIG = {
    "root": os.getenv("HOMMEY_SKILLS_ROOT", ".agents/skills"),
}


RESILIENCE_CONFIG = {
    "max_retries": _int_env("HOMMEY_MAX_RETRIES", 3),
    "agent_max_retries": _int_env("HOMMEY_AGENT_MAX_RETRIES", 1),
    "retry_base_delay_sec": _float_env("HOMMEY_RETRY_BASE_DELAY_SEC", 1.0),
    "retry_max_delay_sec": _float_env("HOMMEY_RETRY_MAX_DELAY_SEC", 30.0),
    "max_agent_calls_per_request": _int_env("HOMMEY_MAX_AGENT_CALLS_PER_REQUEST", 8),
    "max_external_calls_per_request": _int_env("HOMMEY_MAX_EXTERNAL_CALLS_PER_REQUEST", 16),
    "max_external_calls_per_type": _int_env("HOMMEY_MAX_EXTERNAL_CALLS_PER_TYPE", 6),
    # Full planning may include intent recognition, collection, parallel
    # policy/public-info retrieval, planning, and compliance verification.
    "request_timeout_sec": _float_env("HOMMEY_REQUEST_TIMEOUT_SEC", 240.0),
    "circuit_failure_threshold": _int_env("HOMMEY_CIRCUIT_FAILURE_THRESHOLD", 5),
    "circuit_recovery_timeout_sec": _float_env(
        "HOMMEY_CIRCUIT_RECOVERY_TIMEOUT_SEC",
        60.0,
    ),
    "circuit_half_open_successes": _int_env("HOMMEY_CIRCUIT_HALF_OPEN_SUCCESSES", 2),
    "health_check_timeout_sec": _float_env("HOMMEY_HEALTH_CHECK_TIMEOUT_SEC", 10.0),
}


MEMORY_CONFIG = {
    "short_term": {
        "backend": os.getenv("HOMMEY_SHORT_TERM_BACKEND", "memory").lower(),
        "max_turns": _int_env("HOMMEY_SHORT_TERM_MAX_TURNS", 10),
        "session_idle_timeout_sec": _int_env("HOMMEY_SESSION_IDLE_TIMEOUT_SEC", 600),
        "redis_ttl_sec": _int_env("HOMMEY_SESSION_REDIS_TTL_SEC", 86400),
        "redis_host": os.getenv("HOMMEY_REDIS_HOST", "127.0.0.1"),
        "redis_port": _int_env("HOMMEY_REDIS_PORT", 6379),
        "redis_db": _int_env("HOMMEY_REDIS_DB", 0),
        "redis_password": _optional_env("HOMMEY_REDIS_PASSWORD"),
        "redis_key_prefix": os.getenv("HOMMEY_REDIS_KEY_PREFIX", "hommey:short_term"),
    },
    "long_term": {
        "backend": os.getenv("HOMMEY_LONG_TERM_BACKEND", "file").lower(),
        "storage_path": os.getenv("HOMMEY_MEMORY_STORAGE_PATH", "data/memory"),
        "postgres_dsn": os.getenv("HOMMEY_POSTGRES_DSN", ""),
    },
    "safety": {
        "enabled": _bool_env("HOMMEY_MEMORY_SAFETY_ENABLED", True),
    },
    "v2": {
        "enabled": _bool_env("HOMMEY_MEMORY_V2_ENABLED", False),
        "dual_write": _bool_env("HOMMEY_MEMORY_V2_DUAL_WRITE", False),
        "read_mode": os.getenv("HOMMEY_MEMORY_V2_READ_MODE", "legacy").lower(),
    },
}


ATTACHMENT_CONFIG = {
    # 附件原文件存储根目录（容器内挂载卷）。本地开发默认 data/uploads。
    "storage_path": os.getenv("HOMMEY_UPLOADS_PATH", "data/uploads"),
    # 单文件大小上限（字节），默认 25 MB。
    "max_size_bytes": _int_env("HOMMEY_ATTACHMENT_MAX_BYTES", 25 * 1024 * 1024),
    # 单条消息最多附件数。
    "max_per_message": _int_env("HOMMEY_ATTACHMENT_MAX_PER_MESSAGE", 5),
    # 允许的扩展名（小写、不含点）。P0 仅文档类。
    "allowed_extensions": tuple(
        ext.strip().lower().lstrip(".")
        for ext in os.getenv(
            "HOMMEY_ATTACHMENT_ALLOWED_EXTENSIONS",
            "txt,md,docx,pdf",
        ).split(",")
        if ext.strip()
    ),
    # 注入 agent_query 的附件文本总字符预算（超出按来源优先级裁剪）。
    "agent_query_char_budget": _int_env("HOMMEY_AGENT_QUERY_CHAR_BUDGET", 12000),
}


MCP_CONFIG = {
    "auto_connect": _bool_env("HOMMEY_MCP_AUTO_CONNECT", True),
    "connect_timeout": _float_env("HOMMEY_MCP_CONNECT_TIMEOUT", 10.0),
    "servers": {
        "filesystem": {
            "transport": "stdio",
            "command": os.getenv("HOMMEY_MCP_FILESYSTEM_COMMAND", "npx"),
            "args": ["-y", "@anthropic/mcp-server-filesystem", "."],
            "env": {},
            "timeout": _float_env("HOMMEY_MCP_FILESYSTEM_TIMEOUT", 30.0),
            "execution_timeout": _float_env(
                "HOMMEY_MCP_FILESYSTEM_EXECUTION_TIMEOUT",
                60.0,
            ),
            "enabled": _bool_env("HOMMEY_MCP_FILESYSTEM_ENABLED", False),
            "description": (
                "Filesystem operations: read, write, list, and create project files."
            ),
        },
    },
}


# 鉴权系统（JWT + bcrypt）。secret 仅来自环境变量；为 None/空 时由
# webui_new/auth/security.py 在签发/校验前显式抛错，绝不在此硬编码默认值。
AUTH_CONFIG = {
    "jwt_secret": _optional_env("HOMMEY_JWT_SECRET"),
    "jwt_algorithm": os.getenv("HOMMEY_JWT_ALGO", "HS256"),
    "access_expire_minutes": _int_env("HOMMEY_JWT_ACCESS_EXPIRE_MINUTES", 30),
    "refresh_expire_days": _int_env("HOMMEY_JWT_REFRESH_EXPIRE_DAYS", 7),
    "admin_emails": tuple(
        email.strip().lower()
        for email in os.getenv("HOMMEY_ADMIN_EMAILS", "").split(",")
        if email.strip()
    ),
}
