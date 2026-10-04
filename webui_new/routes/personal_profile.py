"""Authenticated settings APIs; profile collection never goes through the agent."""
import logging

from fastapi import APIRouter, Depends

from context.postgres_pool import get_postgres_pool
from context.user_profile_repository import ProfileConflict, UserProfileRepository
from core.user_profile import SavePersonalProfile, SkipPersonalProfile
from settings import MEMORY_CONFIG
from utils.io_executor import run_blocking
from webui_new.auth import User, require_path_user
from webui_new.core.errors import BusinessError, StorageError

logger = logging.getLogger(__name__)


def create_personal_profile_router(repository=None):
    router = APIRouter(prefix="/api/{user_id}/profile")

    def get_repository():
        return repository or UserProfileRepository(get_postgres_pool(MEMORY_CONFIG["long_term"]["postgres_dsn"]))

    async def execute(method, user_id, *args):
        try:
            return await run_blocking(getattr(get_repository(), method), user_id, *args)
        except ProfileConflict:
            raise BusinessError("PROFILE_CONFLICT", "资料已在另一页面更新，请重新打开后编辑", status_code=409)
        except Exception as exc:
            # Never log identifiers, birthdays, project codes or payload values.
            logger.warning("Personal profile storage unavailable error_type=%s", type(exc).__name__)
            raise StorageError("PROFILE_UNAVAILABLE", "个人资料暂时无法保存或读取，请稍后重试")

    @router.get("")
    async def get_profile(user_id: str, current_user: User = Depends(require_path_user)):
        return await execute("get", str(current_user.id))

    @router.put("")
    async def save_profile(user_id: str, data: SavePersonalProfile, current_user: User = Depends(require_path_user)):
        return await execute("save", str(current_user.id), data.profile, data.revision)

    @router.post("/skip")
    async def skip_profile(user_id: str, data: SkipPersonalProfile, current_user: User = Depends(require_path_user)):
        return await execute("skip", str(current_user.id), data.revision)

    return router
