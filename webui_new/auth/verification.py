"""
注册邮箱验证码的 Redis 存储（async）。

- 复用 `utils.redis_coordination.get_redis_coordination_client()`（async 客户端，
  读 `MEMORY_CONFIG.short_term` 的 redis_host/port）；key 前缀 `hommey:verify:`。
- 验证码「覆盖式」存储：重发会覆盖旧码（旧码随之失效），冷却期内不允许多发。
- 不依赖 webui 错误类型：发送频率受限时抛 `RateLimited`，由路由层映射为 429。

限流并发安全（无 GET→SET 竞态窗口）：
- 同邮箱冷却：`SET key 1 EX cooldown NX` 原子抢占，抢不到即冷却中。
- 同 IP 窗口：`INCR` 原子计数，超限 `DECR` 回滚；窗口按时间分片，天然按分钟重置。
- 单邮箱每日：`INCR` 原子计数，超限 `DECR` 回滚；每日 key 按本地日期分片。
"""
import secrets
import time
from datetime import date

from settings import AUTH_CONFIG
from utils.redis_coordination import get_redis_coordination_client

_PREFIX = "hommey:verify"

# verify_code 返回值
VERIFY_OK = "ok"
VERIFY_INVALID = "invalid"            # 无码 / 码错 / 已过期
VERIFY_ATTEMPTS_EXCEEDED = "attempts_exceeded"


class RateLimited(Exception):
    """发送频率受限；`kind` ∈ {"cooldown", "daily_limit", "ip_limit"}。"""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


def _cfg(key: str, default):
    return AUTH_CONFIG.get(key, default)


def _code_key(email: str) -> str:
    return f"{_PREFIX}:code:{email}"


def _attempts_key(email: str) -> str:
    return f"{_PREFIX}:attempts:{email}"


def _cooldown_key(email: str) -> str:
    return f"{_PREFIX}:cooldown:{email}"


def _daily_key(email: str) -> str:
    # 按本地日期分片，自然按天重置；TTL 2 天兜底清理。
    return f"{_PREFIX}:daily:{email}:{date.today().isoformat()}"


def _ip_window_key(ip: str, window_sec: int) -> str:
    # 固定时间窗口分片（当前窗口号 = 时间戳 // 窗口秒数），
    # 窗口自然滚动，无需精确 TTL；TTL 2 个窗口兜底清理历史分片。
    window_id = int(time.time() // max(1, window_sec))
    return f"{_PREFIX}:ip:{ip}:{window_id}"


async def issue_code(email: str, client_ip: str) -> str:
    """生成并存储验证码，返回 6 位数字码；冷却/超限抛 `RateLimited`。

    限流检查全部用 Redis 原子命令完成，多 worker/并发下不会因
    「先 GET 判断、后 SET 写入」的竞态同时放行。
    """
    client = get_redis_coordination_client()
    length = int(_cfg("verification_code_length", 6))
    ttl = int(_cfg("verification_code_ttl_sec", 300))
    cooldown = int(_cfg("verification_code_resend_cooldown_sec", 60))
    daily_limit = int(_cfg("verification_code_daily_limit", 10))
    ip_limit = int(_cfg("verification_ip_rate_limit", 5))
    ip_window = int(_cfg("verification_ip_rate_window_sec", 60))

    # 1) 同 IP 每分钟上限：INCR 原子计数，超限回滚。
    ip_key = _ip_window_key(client_ip, ip_window)
    ip_count = await client.incr(ip_key)
    await client.expire(ip_key, 2 * ip_window)
    if ip_count > ip_limit:
        await client.decr(ip_key)
        raise RateLimited("ip_limit")

    # 2) 同邮箱重发冷却：SET NX 原子抢占，返回 None 说明冷却未结束。
    acquired = await client.set(_cooldown_key(email), "1", ex=cooldown, nx=True)
    if not acquired:
        raise RateLimited("cooldown")

    # 3) 单邮箱每日上限：INCR 原子计数，超限回滚。
    daily_key = _daily_key(email)
    daily = await client.incr(daily_key)
    await client.expire(daily_key, 2 * 86400)
    if daily > daily_limit:
        await client.decr(daily_key)
        raise RateLimited("daily_limit")

    code = str(secrets.randbelow(10 ** length)).zfill(length)

    await client.set(_code_key(email), code, ex=ttl)
    await client.delete(_attempts_key(email))
    return code


async def verify_code(email: str, code: str) -> str:
    """校验验证码；返回 VERIFY_OK / VERIFY_INVALID / VERIFY_ATTEMPTS_EXCEEDED。"""
    client = get_redis_coordination_client()
    max_attempts = int(_cfg("verification_code_max_attempts", 5))
    ttl = int(_cfg("verification_code_ttl_sec", 300))

    stored = await client.get(_code_key(email))
    if stored is None:
        return VERIFY_INVALID

    attempts = await client.incr(_attempts_key(email))
    if attempts == 1:
        await client.expire(_attempts_key(email), ttl)
    if attempts > max_attempts:
        await client.delete(_code_key(email), _attempts_key(email))
        return VERIFY_ATTEMPTS_EXCEEDED

    if not secrets.compare_digest(stored, code):
        return VERIFY_INVALID

    await client.delete(_code_key(email), _attempts_key(email))
    return VERIFY_OK
