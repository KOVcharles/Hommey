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


def _optional_bool_env(name: str, default: bool) -> bool:
    """Read a boolean while allowing a blank env var to inherit its default."""
    value = _optional_env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


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
    # DeepSeek V4 on Alibaba Model Studio enables reasoning by default. Keep it
    # off for the interactive product unless an operator explicitly opts in.
    "enable_thinking": _bool_env("HOMMEY_ENABLE_THINKING", False),
}


COMPOSER_CONFIG = {
    "enabled": _bool_env("HOMMEY_COMPOSER_ENABLED", True),
    "api_key": os.getenv("HOMMEY_COMPOSER_API_KEY") or LLM_CONFIG["api_key"],
    "model_name": os.getenv("HOMMEY_COMPOSER_MODEL_NAME") or LLM_CONFIG["model_name"],
    "base_url": os.getenv("HOMMEY_COMPOSER_BASE_URL") or LLM_CONFIG["base_url"],
    "temperature": _float_env("HOMMEY_COMPOSER_TEMPERATURE", 0.2),
    "max_tokens": _int_env("HOMMEY_COMPOSER_MAX_TOKENS", 4096),
    "enable_thinking": _optional_bool_env(
        "HOMMEY_COMPOSER_ENABLE_THINKING", LLM_CONFIG["enable_thinking"],
    ),
}


TRIP_INTAKE_CONFIG = {
    "enabled": _bool_env("HOMMEY_TRIP_INTAKE_CARD", True),
}


AMAP_CONFIG = {
    "enabled": _optional_bool_env("HOMMEY_AMAP_ENABLED", True),
    "api_key": _optional_env("HOMMEY_AMAP_WEB_KEY"),
    "base_url": os.getenv("HOMMEY_AMAP_BASE_URL", "https://restapi.amap.com").rstrip("/"),
    "timeout_sec": _float_env("HOMMEY_AMAP_TIMEOUT_SEC", 8.0),
    "max_retries": _int_env("HOMMEY_AMAP_MAX_RETRIES", 1),
    "cache_ttl_sec": _int_env("HOMMEY_AMAP_CACHE_TTL_SEC", 300),
    "hotel_radius_m": _int_env("HOMMEY_AMAP_HOTEL_RADIUS_M", 5000),
    "hotel_limit": 3,
    "mainland_only": True,
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
    "vector_backend": os.getenv("HOMMEY_RAG_VECTOR_BACKEND", "postgres").lower(),
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
    # Phase 5: bounded embedding retry/backoff + process-local cache.
    "embedding_max_retries": _int_env("HOMMEY_RAG_EMBEDDING_MAX_RETRIES", 2),
    "embedding_retry_base_delay_sec": _float_env("HOMMEY_RAG_EMBEDDING_RETRY_BASE_DELAY_SEC", 1.0),
    "embedding_retry_max_delay_sec": _float_env("HOMMEY_RAG_EMBEDDING_RETRY_MAX_DELAY_SEC", 30.0),
    "embedding_cache_size": _int_env("HOMMEY_RAG_EMBEDDING_CACHE_SIZE", 1024),
    # Phase 5: BM25/sparse backend seam (only "python" until the corpus
    # outgrows the full-scan baseline; see audit §11 Phase 5).
    "bm25_backend": os.getenv("HOMMEY_RAG_BM25_BACKEND", "python").lower(),
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
    "postgres_dsn": os.getenv("HOMMEY_RAG_POSTGRES_DSN", "")
    or os.getenv("HOMMEY_POSTGRES_DSN", ""),
    "refresh_backend": os.getenv("HOMMEY_RAG_REFRESH_BACKEND", "postgres").lower(),
    "source_storage": os.getenv("HOMMEY_RAG_SOURCE_STORAGE", "shared_filesystem").lower(),
    "refresh_lease_seconds": _int_env("HOMMEY_RAG_REFRESH_LEASE_SECONDS", 90),
    "refresh_max_attempts": _int_env("HOMMEY_RAG_REFRESH_MAX_ATTEMPTS", 3),
    "refresh_retry_delay_seconds": _int_env("HOMMEY_RAG_REFRESH_RETRY_DELAY_SECONDS", 5),
    "refresh_poll_seconds": _float_env("HOMMEY_RAG_REFRESH_POLL_SECONDS", 2.0),
    "refresh_worker_stale_seconds": _int_env("HOMMEY_RAG_REFRESH_WORKER_STALE_SECONDS", 45),
    "chunk_size": _int_env("HOMMEY_RAG_CHUNK_SIZE", 600),
    "chunk_overlap": _int_env("HOMMEY_RAG_CHUNK_OVERLAP", 100),
    "chunk_min_tokens": _int_env("HOMMEY_RAG_CHUNK_MIN_TOKENS", 150),
    "chunk_max_tokens": _int_env("HOMMEY_RAG_CHUNK_MAX_TOKENS", 400),
    "chunk_overlap_tokens": _int_env("HOMMEY_RAG_CHUNK_OVERLAP_TOKENS", 60),
    "top_k": _int_env("HOMMEY_RAG_TOP_K", 3),
    "vector_top_k": _int_env("HOMMEY_RAG_VECTOR_TOP_K", 10),
    "bm25_top_k": _int_env("HOMMEY_RAG_BM25_TOP_K", 10),
    # HyDE is a user-selected retrieval capability. Requests remain on
    # standard retrieval unless the chat payload explicitly asks for enhanced.
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
    # Phase 2: ingestion-accepted file types (comma-separated).  DOCX/CSV/XLSX
    # are new; operators can narrow the set without code changes.
    "supported_file_types": os.getenv(
        "HOMMEY_RAG_SUPPORTED_FILE_TYPES",
        "txt,md,pdf,docx,csv,xlsx",
    ),
    # Phase 3: OCR fallback for text-less PDF pages via the multimodal
    # VisionClient.  Off by default; enable only when a vision API key is set.
    "ocr_enabled": _bool_env("HOMMEY_RAG_OCR_ENABLED", False),
    "ocr_confidence_threshold": _float_env("HOMMEY_RAG_OCR_CONFIDENCE_THRESHOLD", 0.5),
}


TRAIN_QUERY_CONFIG = {
    # 车次查询后端接缝：默认 12306 直连（免费、无 Key）；juhe 为付费/限频预留。
    # 未知后端在 create_train_query_backend 处 fail-fast。
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


EVALUATION_CONFIG = {
    # Shadow evaluation is opt-in and cannot affect the chat response path.
    "enabled": _bool_env("HOMMEY_EVALUATION_ENABLED", False),
    "llm_enabled": _bool_env("HOMMEY_EVALUATION_LLM_ENABLED", False),
    "sample_rate": _float_env("HOMMEY_EVALUATION_SAMPLE_RATE", 0.2),
    "queue_size": _int_env("HOMMEY_EVALUATION_QUEUE_SIZE", 256),
    "worker_concurrency": _int_env("HOMMEY_EVALUATION_WORKER_CONCURRENCY", 2),
    "judge_timeout_sec": _float_env("HOMMEY_EVALUATION_JUDGE_TIMEOUT_SEC", 30.0),
    "judge_model": os.getenv("HOMMEY_EVALUATION_JUDGE_MODEL", ""),
    "judge_api_key": _optional_env("HOMMEY_EVALUATION_API_KEY") or LLM_CONFIG["api_key"],
    "judge_base_url": os.getenv("HOMMEY_EVALUATION_BASE_URL") or LLM_CONFIG["base_url"],
    "context_messages": _int_env("HOMMEY_EVALUATION_CONTEXT_MESSAGES", 4),
    "retention_days": _int_env("HOMMEY_EVALUATION_RETENTION_DAYS", 30),
    "database_pool_size": _int_env("HOMMEY_EVALUATION_DB_POOL_SIZE", 2),
    "database_timeout_sec": _float_env("HOMMEY_EVALUATION_DB_TIMEOUT_SEC", 5.0),
    "lease_seconds": _int_env("HOMMEY_EVALUATION_LEASE_SECONDS", 90),
    "max_attempts": _int_env("HOMMEY_EVALUATION_MAX_ATTEMPTS", 3),
    "evaluator_version": os.getenv("HOMMEY_EVALUATOR_VERSION", "turn-evaluator-v1"),
    "judge_prompt_version": os.getenv("HOMMEY_JUDGE_PROMPT_VERSION", "turn-judge-v1"),
    "rubric_version": os.getenv("HOMMEY_EVALUATION_RUBRIC_VERSION", "travel-rubric-v1"),
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


CONCURRENCY_CONFIG = {
    # 全局并发上限：RedisSemaphore 允许同时进行中的请求数。
    "global_concurrency_limit": _int_env("HOMMEY_GLOBAL_CONCURRENCY_LIMIT", 8),
    # 同用户分布式锁等待超时（秒）。超过则返回用户排队超时。
    "per_user_lock_timeout_sec": _float_env("HOMMEY_PER_USER_LOCK_TIMEOUT_SEC", 60.0),
    # 全局信号量获取超时（秒）。
    "semaphore_acquire_timeout_sec": _float_env("HOMMEY_SEMAPHORE_ACQUIRE_TIMEOUT_SEC", 120.0),
    # 分布式锁 TTL（秒），每次续约重设。
    "distributed_lock_ttl_sec": _float_env("HOMMEY_DISTRIBUTED_LOCK_TTL_SEC", 45.0),
    # 全局信号量单租约 TTL（秒），由心跳续约重设。
    # 与单计数器 TTL 不同：这是每个持有者独立的租约，长请求持续续约即可，
    # 不再要求 >= request_timeout_sec；崩溃持有者的 token 到期后自动回收。
    "semaphore_lease_ttl_sec": _float_env("HOMMEY_SEMAPHORE_LEASE_TTL_SEC", 45.0),
    # 心跳续约间隔（秒）。必须 < 任一租约 TTL 的 1/3，为瞬时延迟留出余量。
    "lock_heartbeat_interval_sec": _float_env("HOMMEY_LOCK_HEARTBEAT_INTERVAL_SEC", 12.0),
    # 拿锁重试 sleep 间隔（秒）。
    "lock_retry_interval_sec": _float_env("HOMMEY_LOCK_RETRY_INTERVAL_SEC", 0.2),
    # 应用自有有界 I/O 线程池上限（RAG embedding / psycopg 等同步阻塞 I/O）。
    # 避免默认 asyncio executor 被突发请求放大、事件循环被同步调用拖慢。
    "io_executor_max_workers": _int_env("HOMMEY_IO_EXECUTOR_MAX_WORKERS", 16),
    # 等待线程执行的最大任务数；超过后立即返回可重试的过载错误。
    "io_executor_max_pending": _int_env("HOMMEY_IO_EXECUTOR_MAX_PENDING", 32),
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
        "backend": os.getenv("HOMMEY_LONG_TERM_BACKEND", "postgres").lower(),
        "storage_path": os.getenv("HOMMEY_MEMORY_STORAGE_PATH", "data/memory"),
        "postgres_dsn": os.getenv("HOMMEY_POSTGRES_DSN", ""),
        "postgres_pool_min_size": _int_env("HOMMEY_POSTGRES_POOL_MIN_SIZE", 1),
        "postgres_pool_max_size": _int_env("HOMMEY_POSTGRES_POOL_MAX_SIZE", 10),
        "postgres_pool_timeout_sec": _float_env("HOMMEY_POSTGRES_POOL_TIMEOUT_SEC", 10.0),
    },
    "retention": {
        "raw_message_days": _int_env("HOMMEY_RAW_MESSAGE_RETENTION_DAYS", 14),
    },
    "safety": {
        "enabled": _bool_env("HOMMEY_MEMORY_SAFETY_ENABLED", True),
    },
    "v2": {
        "enabled": _bool_env("HOMMEY_MEMORY_V2_ENABLED", False),
        "dual_write": _bool_env("HOMMEY_MEMORY_V2_DUAL_WRITE", False),
        "read_mode": os.getenv("HOMMEY_MEMORY_V2_READ_MODE", "legacy").lower(),
    },
    # 增量会话摘要（v1）：读取路径惰性生成，水位推进驱动；max_turns 或 max_chars 谁先到谁触发。
    "summary": {
        "enabled": _bool_env("HOMMEY_SUMMARY_ENABLED", True),
        "max_turns": _int_env("HOMMEY_SUMMARY_MAX_TURNS", 5),
        "max_chars": _int_env("HOMMEY_SUMMARY_MAX_CHARS", 6000),
        "prompt_version": os.getenv("HOMMEY_SUMMARY_PROMPT_VERSION", "segment-v1"),
    },
}


ATTACHMENT_CONFIG = {
    # 附件原文件存储根目录（容器内挂载卷）。本地开发默认 data/uploads。
    "storage_path": os.getenv("HOMMEY_UPLOADS_PATH", "data/uploads"),
    # 单文件大小上限（字节），默认 25 MB。
    "max_size_bytes": _int_env("HOMMEY_ATTACHMENT_MAX_BYTES", 25 * 1024 * 1024),
    # 单条消息最多附件数。
    "max_per_message": _int_env("HOMMEY_ATTACHMENT_MAX_PER_MESSAGE", 5),
    # DOCX 解压上限，阻止超大解压体积和压缩炸弹进入解析器。
    "max_archive_entries": _int_env("HOMMEY_ATTACHMENT_MAX_ARCHIVE_ENTRIES", 2048),
    "max_archive_uncompressed_bytes": _int_env(
        "HOMMEY_ATTACHMENT_MAX_ARCHIVE_BYTES", 100 * 1024 * 1024
    ),
    "max_archive_ratio": _int_env("HOMMEY_ATTACHMENT_MAX_ARCHIVE_RATIO", 100),
    # 原文件的全局保留期；过期附件不能再绑定到新消息。
    "retention_days": _int_env("HOMMEY_ATTACHMENT_RETENTION_DAYS", 30),
    # 允许的扩展名（小写、不含点）。文档类 + 图片（图片需 VISION_CONFIG.enabled）。
    "allowed_extensions": tuple(
        ext.strip().lower().lstrip(".")
        for ext in os.getenv(
            "HOMMEY_ATTACHMENT_ALLOWED_EXTENSIONS",
            "txt,md,docx,pdf,png,jpg,jpeg,webp",
        ).split(",")
        if ext.strip()
    ),
    # 注入 agent_query 的附件文本总字符预算（超出按来源优先级裁剪）。
    "agent_query_char_budget": _int_env("HOMMEY_AGENT_QUERY_CHAR_BUDGET", 12000),
}


VISION_CONFIG = {
    # 图片理解开关。关闭时图片上传被拒（服务层返回 VISION_DISABLED）。
    "enabled": _bool_env("HOMMEY_VISION_ENABLED", False),
    # OpenAI 兼容视觉端点。默认 SiliconFlow；key 复用 SILICONFLOW_API_KEY 或单列。
    "api_key": _optional_env("HOMMEY_VISION_API_KEY") or _optional_env("SILICONFLOW_API_KEY"),
    "base_url": os.getenv("HOMMEY_VISION_BASE_URL", "https://api.siliconflow.cn/v1"),
    "model": os.getenv("HOMMEY_VISION_MODEL", "Qwen/Qwen2.5-VL-72B-Instruct"),
    "timeout_sec": _float_env("HOMMEY_VISION_TIMEOUT_SEC", 30.0),
    # 送入视觉模型的图片降采样上限（总像素），同时限帧式限制请求体。
    "max_pixels": _int_env("HOMMEY_VISION_MAX_PIXELS", 1568 * 1568),
    "max_size_bytes": _int_env("HOMMEY_VISION_MAX_BYTES", 10 * 1024 * 1024),
    # 每用户每日视觉调用配额，防止绕过聊天预算刷视觉 API。
    "daily_limit": _int_env("HOMMEY_VISION_DAILY_LIMIT", 50),
}


OCR_CONFIG = {
    # 文档 OCR 与用户图片理解解耦。Query PDF 和 RAG 扫描页共用这一后端，
    # 但各自保留独立的业务开关与失败语义。
    "enabled": _bool_env("HOMMEY_OCR_ENABLED", False),
    "api_key": _optional_env("HOMMEY_OCR_API_KEY")
    or _optional_env("SILICONFLOW_API_KEY")
    or _optional_env("HOMMEY_VISION_API_KEY"),
    "base_url": os.getenv("HOMMEY_OCR_BASE_URL", "https://api.siliconflow.cn/v1"),
    "model": os.getenv("HOMMEY_OCR_MODEL", "deepseek-ai/DeepSeek-OCR"),
    "timeout_sec": _float_env("HOMMEY_OCR_TIMEOUT_SEC", 30.0),
    "max_retries": _int_env("HOMMEY_OCR_MAX_RETRIES", 2),
    # DeepSeek-OCR 总上下文为 8192；需为视觉输入和提示词保留空间。
    "max_tokens": _int_env("HOMMEY_OCR_MAX_TOKENS", 4096),
    # 仅约束同步 Query PDF 上传；RAG 入库有自己的任务/状态边界。
    "query_pdf_max_pages": _int_env("HOMMEY_QUERY_PDF_OCR_MAX_PAGES", 10),
}


ASR_CONFIG = {
    # 语音转写开关（Mode A：转写为文本后以纯文本发送，不落附件表）。
    "enabled": _bool_env("HOMMEY_ASR_ENABLED", False),
    # OpenAI 兼容 /audio/transcriptions 端点。默认 SiliconFlow。
    "api_key": _optional_env("HOMMEY_ASR_API_KEY") or _optional_env("SILICONFLOW_API_KEY"),
    "base_url": os.getenv("HOMMEY_ASR_BASE_URL", "https://api.siliconflow.cn/v1"),
    "model": os.getenv("HOMMEY_ASR_MODEL", "FunAudioLLM/SenseVoiceSmall"),
    "timeout_sec": _float_env("HOMMEY_ASR_TIMEOUT_SEC", 60.0),
    "max_size_bytes": _int_env("HOMMEY_ASR_MAX_BYTES", 25 * 1024 * 1024),
    "daily_limit": _int_env("HOMMEY_ASR_DAILY_LIMIT", 100),
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
