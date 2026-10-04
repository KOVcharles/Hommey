"""
鉴权用户存储（原生 psycopg，无 ORM）。

连接管理复用 `settings.MEMORY_CONFIG['long_term']['postgres_dsn']`，惯法对齐
`context/long_term_memory.py`（autocommit + dict_row + 短连接）。psycopg 采用
**惰性导入**（仅 `get_conn` 内 `import psycopg`），使本模块在未安装 psycopg 的
环境下仍可被导入——测试用假连接（RecordingConnection 风格）注入即可覆盖，无需真实 PG。

安全要点（PRD §3.1 / §6）：
- 全部 SQL 使用参数化 `%s` 占位，禁止字符串拼接。
- email 作为查询条件，不落日志。
"""
from contextlib import contextmanager
from dataclasses import dataclass

from settings import AUTH_CONFIG, MEMORY_CONFIG
from webui_new.core.errors import ConfigError


@dataclass
class User:
    """users 表行映射。

    `id` 即 canonical user_id（BIGSERIAL），与 JWT `sub` 同源；对外以字符串形式
    出现于 URL 与 token。

    `password_hash` 是登录校验（`verify_password`）所需 的 bcrypt 哈希——它随
    `get_user_by_email`/`get_user_by_id` 一并读出，使 `POST /auth/login` 能在
    不再改存储契约的前提下完成常量时间校验（design.md §3.8 / §9.2）。它**绝不**
    被序列化进任何响应（`UserResponse` 只回 `id/email`），也**绝不**入日志。
    """

    id: int
    email: str
    password_hash: str  # bcrypt 哈希；仅用于登录校验，不外泄、不落日志
    created_at: str  # TIMESTAMPTZ → ISO 字符串
    role: str = "user"


@contextmanager
def get_conn():
    """鉴权短连接 context manager。

    DSN 缺失（None/空）时抛 `ConfigError`（对应 PRD §3.1 “如该 DSN 为空须给出清晰错误”）。
    鉴权是请求级、低频操作，v1.0 无需连接池（与 long_term_memory.py 一致）。
    """
    import psycopg
    from psycopg.rows import dict_row

    dsn = MEMORY_CONFIG.get("long_term", {}).get("postgres_dsn", "")
    if not dsn:
        raise ConfigError(
            "AUTH_STORE_UNCONFIGURED",
            "鉴权存储未配置（缺少 HOMMEY_POSTGRES_DSN），请联系管理员",
        )
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


_USERS_DDL = (
    "CREATE TABLE IF NOT EXISTS users ("
    " id            BIGSERIAL    PRIMARY KEY,"
    " email         TEXT         UNIQUE NOT NULL,"
    " password_hash TEXT         NOT NULL,"
    " role          TEXT         NOT NULL DEFAULT 'user',"
    " created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()"
    ")"
)

_INVITES_DDL = (
    "CREATE TABLE IF NOT EXISTS invite_codes ("
    " code_hash CHAR(64) PRIMARY KEY,"
    " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
    " used_at TIMESTAMPTZ,"
    " used_by_user_id BIGINT REFERENCES users(id)"
    ")"
)

# 含 password_hash：登录需读出哈希做常量时间校验（design.md §3.8），
# 故 email/id 两条查询路径都带回它；明文密码从不落库，自然不会出现在列里。
_USER_COLUMNS = "id, email, password_hash, created_at, role"


def apply_migration(conn) -> None:
    """幂等创建用户与邀请码表。"""
    with conn.cursor() as cur:
        cur.execute(_USERS_DDL)
        cur.execute(_INVITES_DDL)


def _row_to_user(row: dict | None) -> User | None:
    if not row:
        return None
    return User(
        id=row["id"],
        email=row["email"],
        password_hash=row["password_hash"],
        created_at=row["created_at"],
        role=row.get("role", "user"),
    )


def create_user(conn, email: str, password_hash: str) -> User:
    """插入新用户并返回新行。

    重复 email 由 route 层预先 `get_user_by_email` 判重映射为 409
    （`EMAIL_ALREADY_EXISTS`）；本函数假设 email 尚未占用。
    """
    role = "admin" if email.strip().lower() in AUTH_CONFIG.get("admin_emails", ()) else "user"
    sql = (
        "INSERT INTO users (email, password_hash, role) VALUES (%s, %s, %s)"
        f" RETURNING {_USER_COLUMNS}"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (email, password_hash, role))
        row = cur.fetchone()
    return _row_to_user(row)


def get_user_by_email(conn, email: str) -> User | None:
    """按邮箱查用户（注册判重 / 登录校验）。

    返回的 `User` 含 `password_hash`，供 route 层 `verify_password` 做常量时间
    校验（design.md §3.8）；明文密码从不落库，故 row 中只有哈希。
    """
    sql = f"SELECT {_USER_COLUMNS} FROM users WHERE email = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (email,))
        row = cur.fetchone()
    return _row_to_user(row)


def get_user_by_id(conn, user_id: int) -> User | None:
    """按 DB id 查用户。

    PRD §3.1 未列此函数，但 `deps.get_current_user` 必需（token `sub` 是 user id，
    需按 id 查库确认用户仍存在）。这是对访问层的最小、自然补充。
    """
    sql = f"SELECT {_USER_COLUMNS} FROM users WHERE id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (user_id,))
        row = cur.fetchone()
    return _row_to_user(row)
