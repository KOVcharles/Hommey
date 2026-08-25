"""
页面路由。

只负责返回 HTML 页面，不处理业务状态。模板渲染函数由 server.py 注入，
这样本模块不需要知道模板目录和 Jinja 环境怎么创建。
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse


def create_pages_router(render):
    """创建页面 router；render 是 server.py 提供的模板渲染函数。"""
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def login_page():
        """登录页"""
        return render("login.html")

    @router.get("/chat/{user_id}", response_class=HTMLResponse)
    async def chat_page(user_id: int):
        """聊天主页面"""
        return render("chat.html", user_id=str(user_id))

    @router.get("/admin/skills", response_class=HTMLResponse)
    async def skill_admin_page():
        """管理员 Skill 平台页面；数据 API 仍由 JWT 管理员权限保护。"""
        return render("admin_skills.html")

    return router
