"""
Hommey 商旅助手 - FastAPI Web 服务入口

这个文件只负责组装 Web 应用：
- 创建 FastAPI app
- 挂载静态资源和模板渲染器
- 创建共享的 WebHommeyManager
- 注册 middleware、exception handler 和各个 router

具体 API 逻辑放在 webui_new/routes/，请求模型放在 webui_new/schemas/，
错误响应和 request_id 逻辑放在 webui_new/core/。
"""
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from settings import MEMORY_CONFIG, RAG_CONFIG, SYSTEM_CONFIG
from utils.observability import render_metrics
from utils.preflight import run_preflight
from utils.structured_logging import configure_logging
from webui_new.core.errors import register_error_handlers
from webui_new.manager import WebHommeyManager
from webui_new.routes.auth import create_auth_router
from webui_new.routes.attachments import create_attachments_router
from webui_new.routes.chat import create_chat_router
from webui_new.routes.onboarding import create_onboarding_router
from webui_new.routes.pages import create_pages_router
from webui_new.routes.users import create_users_router
from webui_new.auth.migrations import apply_all_migrations
from webui_new.routes.skill_admin import create_skill_admin_router
from webui_new.routes.knowledge_base import create_knowledge_base_router
from webui_new.skill_platform import SkillPlatformService
from context.postgres_pool import close_all_postgres_pools


configure_logging()

@asynccontextmanager
async def lifespan(_app):
    if MEMORY_CONFIG.get("long_term", {}).get("backend") == "postgres":
        apply_all_migrations()
    try:
        yield
    finally:
        close_all_postgres_pools()


app = FastAPI(title="Hommey 商旅助手", version="3.0.0", lifespan=lifespan)
manager = WebHommeyManager()
skill_platform = SkillPlatformService()

# 多模态附件服务（与 runtime 共享同一单例：路由用于上传，manager 用于 normalize/绑定）。
from runtime import get_shared_attachment_service  # noqa: E402

attachment_service = get_shared_attachment_service()

static_dir = os.path.join(os.path.dirname(__file__), "static")
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
os.makedirs(static_dir, exist_ok=True)
os.makedirs(templates_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")
jinja_env = Environment(loader=FileSystemLoader(templates_dir), enable_async=False)


def _render(template_name: str, **context) -> HTMLResponse:
    """渲染 templates/ 下的页面，供 pages router 注入使用。"""
    template = jinja_env.get_template(template_name)
    return HTMLResponse(template.render(**context))


# 注册顺序保持简单：先全局错误处理，再挂载功能路由。
register_error_handlers(app)
app.include_router(create_pages_router(_render))
app.include_router(create_auth_router())
app.include_router(create_users_router(manager))
app.include_router(create_onboarding_router(manager))
app.include_router(create_chat_router(manager))
app.include_router(create_attachments_router(attachment_service))
app.include_router(create_skill_admin_router(skill_platform))
documents_dir = Path(RAG_CONFIG["documents_dir"])
if not documents_dir.is_absolute():
    documents_dir = Path(project_root) / documents_dir
knowledge_base_path = Path(RAG_CONFIG["knowledge_base_path"])
if not knowledge_base_path.is_absolute():
    knowledge_base_path = Path(project_root) / knowledge_base_path
app.include_router(create_knowledge_base_router(documents_dir, knowledge_base_path))


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    include_network = bool(SYSTEM_CONFIG.get("preflight_include_network", False))
    return await run_preflight(include_network=include_network)


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return PlainTextResponse(render_metrics(), media_type="text/plain; version=0.0.4; charset=utf-8")
