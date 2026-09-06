"""
Resend 事务邮件发送器（注册邮箱验证码）。

- 走 Resend REST API（`POST https://api.resend.com/emails`），复用已有 `httpx`，
  不引入 `resend` SDK。
- `HOMMEY_RESEND_API_KEY` 缺失（None/空）时抛 `ConfigError`，绝不硬编码默认值；
  与 security.py 的 JWT secret 守护保持一致。
- 用 `Idempotency-Key` 头防重发；发送失败抛 `UpstreamError`，由路由层转统一错误响应。
"""
import logging
import uuid

import httpx

from settings import EMAIL_CONFIG
from webui_new.core.errors import ConfigError, UpstreamError

logger = logging.getLogger(__name__)


def _verification_html(code: str) -> str:
    """验证码邮件 HTML；code 为纯数字，直接文本插入，无注入风险。"""
    return (
        '<!doctype html><html lang="zh-CN"><body style="font-family:sans-serif;line-height:1.6">'
        '<h2>Hommey 注册验证码</h2>'
        "<p>你的注册验证码是：</p>"
        f'<p style="font-size:32px;font-weight:700;letter-spacing:8px">{code}</p>'
        "<p>验证码 5 分钟内有效，请勿泄露给他人。</p>"
        "</body></html>"
    )


async def send_verification_email(to_email: str, code: str) -> None:
    """通过 Resend 发送一封验证码邮件；失败抛 `ConfigError` 或 `UpstreamError`。"""
    api_key = EMAIL_CONFIG.get("resend_api_key")
    if not api_key:
        raise ConfigError(
            "EMAIL_NOT_CONFIGURED",
            "邮件发送未配置（缺少 HOMMEY_RESEND_API_KEY），请联系管理员",
        )

    payload = {
        "from": EMAIL_CONFIG.get("resend_from") or "onboarding@resend.dev",
        "to": [to_email],
        "subject": "Hommey 注册验证码",
        "html": _verification_html(code),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Idempotency-Key": uuid.uuid4().hex,
    }
    endpoint = EMAIL_CONFIG.get("resend_endpoint") or "https://api.resend.com/emails"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        logger.error("resend request failed email=%s error=%s", to_email, exc)
        raise UpstreamError("EMAIL_SEND_FAILED", "验证码邮件发送失败，请稍后重试") from exc

    if resp.status_code >= 400:
        logger.error(
            "resend rejected email=%s status=%s", to_email, resp.status_code
        )
        raise UpstreamError("EMAIL_SEND_FAILED", "验证码邮件发送失败，请稍后重试")
