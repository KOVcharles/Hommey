"""Administrator-only Skill platform API."""
from fastapi import APIRouter, Depends

from webui_new.auth import User, require_admin
from webui_new.core.errors import BusinessError


def create_skill_admin_router(service):
    router = APIRouter(prefix="/api/admin/skills", tags=["skill-admin"])

    @router.get("")
    async def list_skills(current_user: User = Depends(require_admin)):
        return {"skills": service.list_skills()}

    @router.get("/{skill_name}")
    async def get_skill(skill_name: str, current_user: User = Depends(require_admin)):
        skill = service.get_skill(skill_name)
        if not skill:
            raise BusinessError("SKILL_NOT_FOUND", "Skill 不存在", status_code=404)
        return skill

    return router
