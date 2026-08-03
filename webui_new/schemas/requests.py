"""
WebUI API 请求体模型。

这里只放入站 request body 的 Pydantic schema，避免路由文件里散落模型定义。
响应结构暂时保持现状，没有在这里建 response schema。
"""
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    user_id: str


class ChatRequest(BaseModel):
    # message 与 attachment_ids 不能同时为空（路由层校验）。默认值保证老客户端兼容。
    message: str = ""
    attachment_ids: list[str] = Field(default_factory=list)
    client_request_id: str | None = None


class SessionRenameRequest(BaseModel):
    title: str


class SkillToggleRequest(BaseModel):
    enabled: bool


class OnboardingPreferenceRequest(BaseModel):
    key: str
    value: str


# ---------------------------------------------------------------------------
# 鉴权（v1.0，design.md §3.7）：注册 / 登录 / 刷新 的请求与响应模型。
# 测试阶段暂不限制邮箱格式与密码长度；是否为空由路由统一检查并返回友好提示。
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """注册 / JWT 登录入参：{email, password}（登录复用同一形状，见 design.md §3.8）。"""

    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    """登录/刷新返回体（严格遵循 PRD §3.4，不含 id；canonical id 由 token sub 承载）。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    """注册成功返回体（仅 id/email；password_hash 与明文绝不外泄）。"""

    id: int
    email: str
