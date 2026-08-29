"""
WebUI API 请求体模型。

这里只放入站 request body 的 Pydantic schema，避免路由文件里散落模型定义。
响应结构暂时保持现状，没有在这里建 response schema。
"""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LoginRequest(BaseModel):
    user_id: str


QuickTripCapability = Literal["weather", "local_transport", "train", "nearby_hotels"]


class TripInput(BaseModel):
    """Validated fields supplied by the quick-trip form.

    This is an input adapter only. The values still enter the ordinary trip
    collection and orchestration workflow; the form is not a second planner.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    origin: str = Field(min_length=1, max_length=100)
    destination: str = Field(min_length=1, max_length=100)
    start_date: date
    end_date: date
    duration_days: int | None = Field(default=None, ge=1, le=60)
    trip_purpose: str = Field(min_length=1, max_length=300)
    work_location: str = Field(default="", max_length=200)
    work_location_note: str = Field(default="", max_length=300)
    work_location_place_id: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def validate_trip_range(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        calculated = (self.end_date - self.start_date).days + 1
        if calculated > 60:
            raise ValueError("trip date range cannot exceed 60 days")
        if self.duration_days is not None and self.duration_days != calculated:
            raise ValueError("duration_days must match start_date and end_date")
        self.duration_days = calculated
        if bool(self.work_location) != bool(self.work_location_place_id):
            raise ValueError("work_location and work_location_place_id must be provided together")
        return self


class CapabilitySelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include: list[QuickTripCapability] = Field(default_factory=list, max_length=4)
    exclude: list[QuickTripCapability] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_selection(self):
        self.include = list(dict.fromkeys(self.include))
        self.exclude = list(dict.fromkeys(self.exclude))
        overlap = set(self.include) & set(self.exclude)
        if overlap:
            raise ValueError("a capability cannot be both included and excluded")
        return self


class ChatRequest(BaseModel):
    # message 与 attachment_ids 不能同时为空（路由层校验）。默认值保证老客户端兼容。
    message: str = ""
    attachment_ids: list[str] = Field(default_factory=list)
    client_request_id: str | None = None
    # 新客户端在每次请求中明确会话归属；未传时兼容旧客户端的当前会话行为。
    session_id: str | None = None
    retrieval_mode: Literal["standard", "enhanced"] = "standard"
    input_source: Literal["chat", "quick_trip_form"] = "chat"
    trip_input: TripInput | None = None
    capability_selection: CapabilitySelectionRequest | None = None

    @model_validator(mode="after")
    def validate_input_source(self):
        if self.input_source == "quick_trip_form" and self.trip_input is None:
            raise ValueError("trip_input is required for quick_trip_form")
        if self.input_source == "chat" and self.trip_input is not None:
            raise ValueError("trip_input is only accepted for quick_trip_form")
        return self


class SessionRenameRequest(BaseModel):
    title: str


class InterruptRequest(BaseModel):
    client_request_id: str
    session_id: str | None = None


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
