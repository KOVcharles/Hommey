"""
Configuration reference for the Hommey multi-agent system.

Runtime code reads settings.py, which loads values from environment variables.
Do not put real API keys in this file.
"""
import os


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


SYSTEM_CONFIG = {
    "enable_llm": _bool_env("HOMMEY_ENABLE_LLM", True),
    "log_level": os.getenv("HOMMEY_LOG_LEVEL", "INFO"),
    "max_retries": _int_env("HOMMEY_SYSTEM_MAX_RETRIES", 3),
    "timeout": _int_env("HOMMEY_TIMEOUT", 60),
}


RAG_CONFIG = {
    "embedding_model": os.getenv(
        "HOMMEY_EMBEDDING_MODEL",
        "data/models/bge-small-zh-v1.5",
    ),
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
    "hyde_enabled": _bool_env("HOMMEY_RAG_HYDE_ENABLED", True),
    "hyde_timeout_sec": _float_env("HOMMEY_RAG_HYDE_TIMEOUT_SEC", 12.0),
    "hyde_max_chars": _int_env("HOMMEY_RAG_HYDE_MAX_CHARS", 600),
    "hyde_candidate_top_k": _int_env("HOMMEY_RAG_HYDE_CANDIDATE_TOP_K", 10),
    "hyde_rrf_weight": _float_env("HOMMEY_RAG_HYDE_RRF_WEIGHT", 0.6),
    "hyde_prompt_version": os.getenv("HOMMEY_RAG_HYDE_PROMPT_VERSION", "hyde-policy-v1"),
    "hyde_trace_file": os.getenv(
        "HOMMEY_RAG_HYDE_TRACE_FILE",
        "data/rag_knowledge/hyde_traces.jsonl",
    ),
}


TRAIN_QUERY_CONFIG = {
    "backend": os.getenv("HOMMEY_TRAIN_QUERY_BACKEND", "12306").lower(),
    "juhe_train_key": _optional_env("HOMMEY_JUHE_TRAIN_KEY"),
    "timeout_sec": _float_env("HOMMEY_TRAIN_QUERY_TIMEOUT_SEC", 10.0),
    "max_retries": _int_env("HOMMEY_TRAIN_QUERY_MAX_RETRIES", 2),
    "retry_base_delay_sec": _float_env("HOMMEY_TRAIN_QUERY_RETRY_BASE_DELAY_SEC", 1.0),
    "retry_max_delay_sec": _float_env("HOMMEY_TRAIN_QUERY_RETRY_MAX_DELAY_SEC", 10.0),
    "station_cache_ttl_sec": _int_env("HOMMEY_TRAIN_STATION_CACHE_TTL_SEC", 604800),
}


SKILL_CONFIG = {
    "root": os.getenv("HOMMEY_SKILLS_ROOT", ".agents/skills"),
}


RESILIENCE_CONFIG = {
    "max_retries": _int_env("HOMMEY_MAX_RETRIES", 3),
    "retry_base_delay_sec": _float_env("HOMMEY_RETRY_BASE_DELAY_SEC", 1.0),
    "retry_max_delay_sec": _float_env("HOMMEY_RETRY_MAX_DELAY_SEC", 30.0),
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
