"""
新用户初始化偏好 API。

这里保留 onboarding 的 HTTP 路由和错误边界，实际偏好保存逻辑仍委托给
HommeyWebInstance/InitialPreferenceOnboarding，避免路由层变厚。
"""
import logging

from fastapi import APIRouter, Depends, Request

from utils.logging_safety import sanitize_for_log
from webui_new.auth import User, require_path_user
from webui_new.core.errors import BusinessError, StorageError, ValidationError, request_id
from webui_new.schemas.requests import OnboardingPreferenceRequest

logger = logging.getLogger(__name__)


def create_onboarding_router(manager):
    """创建 onboarding router，路径保持和拆分前一致。"""
    router = APIRouter()

    @router.get("/api/{user_id}/onboarding")
    async def get_onboarding_state(request: Request, user_id: str, current_user: User = Depends(require_path_user)):
        """获取新用户初始化偏好进度"""
        try:
            instance = await manager.get_initialized_user(user_id)
            return await instance.get_onboarding_state()
        except Exception as e:
            logger.error("Onboarding state failed request_id=%s user_id=%s error=%s", request_id(request), user_id, sanitize_for_log(e))
            return {"is_new": True, "completed": False, "missing_keys": []}

    @router.post("/api/{user_id}/onboarding/preference")
    async def save_onboarding_preference(
        request: Request, user_id: str, data: OnboardingPreferenceRequest, current_user: User = Depends(require_path_user)
    ):
        """保存新用户初始化偏好，不经过普通聊天链路"""
        try:
            async with manager.user_state_scope(user_id) as instance:
                return await instance.save_onboarding_preference(data.key, data.value)
        except ValueError as e:
            logger.warning("Onboarding validation failed request_id=%s user_id=%s error=%s", request_id(request), user_id, sanitize_for_log(e))
            raise ValidationError("INVALID_ONBOARDING_PREFERENCE", "偏好项不支持，请刷新页面后重试")
        except Exception as e:
            logger.error("Onboarding preference failed request_id=%s user_id=%s error=%s", request_id(request), user_id, sanitize_for_log(e))
            raise StorageError("ONBOARDING_SAVE_FAILED", "保存初始化偏好失败，请稍后重试")

    return router
