"""
/auth/register | /auth/login | /auth/refresh 端到端测试（design.md §9.3）。

用 fake 存储替换 `webui_new.routes.auth` 模块里导入的 storage 函数，避免依赖真实 PG；
但 **密码哈希 / JWT 签发校验走真实实现**（passlib bcrypt + PyJWT），从而覆盖真正的
加密链路。每个对外接口至少覆盖正常路径与主要错误路径。
"""
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webui_new.auth import security
from webui_new.auth.storage import User
from webui_new.core.errors import register_error_handlers
from webui_new.routes.auth import create_auth_router
import webui_new.routes.auth as auth_routes

_TEST_SECRET = "test-secret-not-for-production-0123456789"


class _FakeStore:
    """内存用户存储；同时扮演 get_conn() 返回的 context manager（conn 被各函数忽略）。"""

    def __init__(self):
        self.by_email: dict[str, User] = {}
        self._next_id = 1

    def get_conn(self):
        return self  # with X as conn -> conn = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def apply_migration(self, conn):
        pass  # 无需建表

    def get_user_by_email(self, conn, email):
        return self.by_email.get(email)

    def create_user(self, conn, email, password_hash):
        uid = self._next_id
        self._next_id += 1
        user = User(id=uid, email=email, password_hash=password_hash, created_at="2026-01-01T00:00:00+00:00")
        self.by_email[email] = user
        return user


@pytest.fixture
def client(monkeypatch):
    """TestClient + 注入确定性 secret + fake 存储。"""
    monkeypatch.setitem(security.AUTH_CONFIG, "jwt_secret", _TEST_SECRET)
    store = _FakeStore()
    monkeypatch.setattr(auth_routes, "get_conn", store.get_conn)
    monkeypatch.setattr(auth_routes, "apply_migration", store.apply_migration)
    monkeypatch.setattr(auth_routes, "get_user_by_email", store.get_user_by_email)
    monkeypatch.setattr(auth_routes, "create_user", store.create_user)

    async def _fake_verify(email, code):
        return "ok"  # 本套用例只测 register 的落库/查重/错误映射，验证码校验另测

    monkeypatch.setattr(auth_routes, "verify_code", _fake_verify)

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(create_auth_router())
    return TestClient(app), store


def _register(c, email="alice@example.com", password="supersecret-123", code="123456"):
    return c.post("/auth/register", json={"email": email, "password": password, "code": code})


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------

def test_register_returns_201_and_user_without_password(client):
    c, _ = client
    r = _register(c)
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "alice@example.com"
    assert isinstance(body["id"], int)
    # 响应绝不回密码 / 哈希
    assert "password" not in body
    assert "password_hash" not in body


def test_register_duplicate_email_is_409(client):
    c, _ = client
    _register(c, email="bob@example.com")
    r = _register(c, email="bob@example.com", password="another-pwd-456")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "EMAIL_ALREADY_EXISTS"


def test_register_accepts_nonstandard_email_for_testing(client):
    c, _ = client
    r = c.post("/auth/register", json={"email": "test-user", "password": "supersecret-123", "code": "123456"})
    assert r.status_code == 201
    assert r.json()["email"] == "test-user"


def test_register_accepts_short_password_for_testing(client):
    c, _ = client
    r = c.post("/auth/register", json={"email": "x", "password": "1", "code": "123456"})
    assert r.status_code == 201


@pytest.mark.parametrize("email,password", [("", "1"), ("x", "")])
def test_register_still_rejects_empty_credentials(client, email, password):
    c, _ = client
    r = c.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------

def test_login_returns_access_and_refresh_tokens(client):
    c, _ = client
    _register(c)
    r = c.post("/auth/login", json={"email": "alice@example.com", "password": "supersecret-123"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    # 登录响应不回 id（canonical id 由 token sub 承载）
    assert "id" not in body


def test_login_accepts_nonstandard_email_and_short_password(client):
    c, _ = client
    _register(c, email="dev-user", password="1")
    r = c.post("/auth/login", json={"email": "dev-user", "password": "1"})
    assert r.status_code == 200


def test_login_wrong_password_is_401(client):
    c, _ = client
    _register(c)
    r = c.post("/auth/login", json={"email": "alice@example.com", "password": "wrong-password-9"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


def test_login_unknown_user_is_401(client):
    c, _ = client
    r = c.post("/auth/login", json={"email": "nobody@example.com", "password": "supersecret-123"})
    assert r.status_code == 401


def test_login_response_does_not_leak_which_field_wrong(client):
    """401 文案不区分「用户不存在」与「密码错误」（防邮箱枚举）。"""
    c, _ = client
    _register(c)
    r1 = c.post("/auth/login", json={"email": "alice@example.com", "password": "wrong-password-9"})
    r2 = c.post("/auth/login", json={"email": "nobody@example.com", "password": "supersecret-123"})
    assert r1.json()["error"]["message"] == r2.json()["error"]["message"]


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------

def _login(c, email="alice@example.com", password="supersecret-123"):
    _register(c, email=email)
    return c.post("/auth/login", json={"email": email, "password": password}).json()


def test_refresh_returns_new_access_token(client):
    c, _ = client
    tokens = _login(c)
    r = c.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"]
    payload = jwt.decode(body["access_token"], _TEST_SECRET, algorithms=["HS256"])
    assert payload["type"] == "access"


def test_refresh_rejects_access_token_used_as_refresh(client):
    c, _ = client
    tokens = _login(c)
    r = c.post("/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert r.status_code == 401


def test_refresh_rejects_garbage_token(client):
    c, _ = client
    r = c.post("/auth/refresh", json={"refresh_token": "not-a-jwt"})
    assert r.status_code == 401


def test_access_and_refresh_types_are_distinct(client):
    c, _ = client
    tokens = _login(c)
    access = jwt.decode(tokens["access_token"], _TEST_SECRET, algorithms=["HS256"])
    refresh = jwt.decode(tokens["refresh_token"], _TEST_SECRET, algorithms=["HS256"])
    assert access["type"] == "access"
    assert refresh["type"] == "refresh"
    assert access["sub"] == refresh["sub"]  # 同一用户
