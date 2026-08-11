"""
鉴权依赖：get_current_user + require_path_user（design.md §3.4 / §5）。

- `get_current_user`：解析 access token（type==access）并按 sub 查库返回用户；
  任一失败（无 token / 解码失败 / type 错 / sub 缺失 / 用户不存在）统一 → 401，
  文案不泄露具体原因（防邮箱/凭据枚举探针）。
- `require_path_user`：在 `get_current_user` 之上叠加「身份一致性」——路径 {user_id}
  必须等于认证身份，否则 403 FORBIDDEN。作为受保护端点的【唯一】依赖注入点，
  handler 函数体无需任何改动（严格满足 Planner「仅注入、不重写业务」）。
"""
import jwt
from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

from webui_new.auth.security import decode_token
from webui_new.auth.storage import User, get_conn, get_user_by_id
from webui_new.core.errors import BusinessError

# auto_error=False：自己控制 401 文案与 error.code（UNAUTHORIZED），不让 FastAPI
# 默认 "Not authenticated" 走 BAD_REQUEST（400）路径（design.md §3.4 要点）。
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def _unauthorized() -> BusinessError:
    """统一 401 错误；文案不区分原因（防枚举）。"""
    return BusinessError("UNAUTHORIZED", "未登录或登录已过期，请重新登录", status_code=401)


async def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    """解码 access token 并按 sub(DB id) 查库返回当前用户；任一失败 → 401。"""
    if not token:
        raise _unauthorized()
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        raise _unauthorized()

    # 防 refresh token 被当作 access 使用。
    if payload.get("type") != "access":
        raise _unauthorized()

    try:
        sub = int(payload["sub"])
        with get_conn() as conn:
            user = get_user_by_id(conn, sub)
    except (KeyError, ValueError, TypeError):
        raise _unauthorized()

    if not user:
        raise _unauthorized()
    return user


async def require_path_user(
    user_id: str,
    current_user: User = Depends(get_current_user),
) -> User:
    """鉴权 + 横向越权（IDOR）防护：路径 {user_id} 必须等于认证身份。

    - 未登录 / token 无效 → get_current_user 抛 401；
    - 已登录但 path user_id ≠ current_user.id → 这里抛 403 FORBIDDEN；
    - 一致 → 返回 current_user，handler 函数体无需任何改动。

    `user_id` 由 FastAPI 从路径参数解析（与端点签名同名同源），无需额外接线。
    """
    if str(user_id) != str(current_user.id):
        raise BusinessError("FORBIDDEN", "无权访问该用户的数据", status_code=403)
    return current_user


async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Allow privileged management APIs only for authenticated administrators."""
    if current_user.role != "admin":
        raise BusinessError("FORBIDDEN", "仅管理员可以使用此管理功能", status_code=403)
    return current_user
