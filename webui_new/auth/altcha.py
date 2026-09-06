"""
ALTCHA 自托管人机验证（注册发码前必过）。

- challenge 由后端生成并以 HMAC 签名，前端组件从 `GET /auth/altcha-challenge` 拉取；
  浏览器侧完成工作量证明（PoW）后提交 base64 payload，本模块离线校验，
  全程不依赖任何第三方服务，不发起外部请求。
- `ALTCHA_HMAC_KEY` 缺失（None/空）时抛 `ConfigError`，绝不硬编码默认值；
  与 email.py 的 Resend key 守护保持一致。
- 校验不通过抛 `BusinessError`（400，前端据此重置组件并要求重新验证）。
"""
import logging
import time

from altcha import create_challenge, verify_solution

from settings import ALTCHA_CONFIG
from webui_new.core.errors import BusinessError, ConfigError

logger = logging.getLogger(__name__)

# 算法与工作量参数：SHA-256 代价 1，key 前缀 20 位零 → 期望约 50 万次哈希，
# 浏览器 worker 内约 0.5-1 秒，服务端校验只需 1 次哈希。
_ALGORITHM = "SHA-256"
_COST = 1
_KEY_PREFIX = "00000"


def _hmac_key() -> str:
    key = ALTCHA_CONFIG.get("hmac_key")
    if not key:
        raise ConfigError(
            "ALTCHA_NOT_CONFIGURED",
            "人机验证未配置（缺少 ALTCHA_HMAC_KEY），请联系管理员",
        )
    return key


def create_altcha_challenge() -> dict:
    """生成带签名的 challenge 字典（含 expiresAt，默认 5 分钟有效）。"""
    challenge = create_challenge(
        algorithm=_ALGORITHM,
        cost=_COST,
        key_prefix=_KEY_PREFIX,
        expires_at=int(time.time()) + int(ALTCHA_CONFIG.get("challenge_expiry_sec", 300)),
        hmac_secret=_hmac_key(),
    )
    return challenge.to_dict()


def verify_altcha_solution(payload: str) -> None:
    """校验前端提交的 base64 payload；通过时返回 None，失败抛错。

    payload 一次性生成、5 分钟内有效；不校验 remote_ip（本地验证无此概念）。
    """
    # key 缺失的 ConfigError 必须在防御块之外抛出：未配置时 fail-closed 返回 500。
    key = _hmac_key()
    try:
        result = verify_solution(payload, hmac_secret=key)
    except Exception as exc:
        # 防御：畸形 payload（解码失败等）一律按未通过处理，不泄露内部细节。
        logger.warning("altcha payload unparsable error=%s", type(exc).__name__)
        raise BusinessError(
            "INVALID_ALTCHA", "人机验证未通过，请重试", status_code=400
        ) from exc
    if not result.verified:
        logger.warning(
            "altcha rejected expired=%s invalid_signature=%s invalid_solution=%s error=%s",
            result.expired,
            result.invalid_signature,
            result.invalid_solution,
            result.error,
        )
        raise BusinessError(
            "INVALID_ALTCHA", "人机验证未通过，请重试", status_code=400
        )
