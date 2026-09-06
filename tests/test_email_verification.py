"""
注册邮箱验证码端到端测试（design.md §3.8，两步流程 + 自托管 ALTCHA 人机验证）。

- `GET /auth/altcha-challenge`：返回带 HMAC 签名的 challenge（algorithm/cost/keyPrefix/expiresAt）。
- `POST /auth/send-verification-code`：发码（ALTCHA 离线校验 + Redis 限流 + 真发邮件）；
  缺 payload / 校验拒绝 / 过期 → 400；冷却 / 同 IP 限流 / 每日限流 → 429；
  已注册邮箱仍 200 但不真发。

替换策略：fake 存储 + 内存假 Redis（`verification.get_redis_coordination_client`）
+ 假邮件发送器（`auth_routes.send_verification_email`）。**ALTCHA challenge 的
生成、PoW 求解与 payload 校验走真实 `altcha` 库**，仅 HMAC key 换成测试值、
PoW 前缀从 `00000` 缩短为 `00`（测试内求解瞬时完成），从而覆盖真实的
人机验证 + 限流 + 码校验链路。
"""
import json
import time

import pytest
from altcha import Challenge, Payload, create_challenge, solve_challenge
from fastapi import FastAPI
from fastapi.testclient import TestClient

import settings
from webui_new.auth.storage import User
from webui_new.core.errors import BusinessError, register_error_handlers
from webui_new.routes.auth import create_auth_router
import webui_new.auth.altcha as altcha_mod
import webui_new.auth.verification as verification
import webui_new.routes.auth as auth_routes

TEST_HMAC_KEY = "test-hmac-key"


class _FakeStore:
    """内存用户存储；同时扮演 get_conn() 返回的 context manager。"""

    def __init__(self):
        self.by_email: dict[str, User] = {}
        self._next_id = 1

    def get_conn(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def apply_migration(self, conn):
        pass

    def get_user_by_email(self, conn, email):
        return self.by_email.get(email)

    def create_user(self, conn, email, password_hash):
        uid = self._next_id
        self._next_id += 1
        user = User(id=uid, email=email, password_hash=password_hash, created_at="2026-01-01T00:00:00+00:00")
        self.by_email[email] = user
        return user


class _FakeRedis:
    """内存 async Redis，仅实现 verification.py 用到的命令子集（无 TTL 语义）。"""

    def __init__(self):
        self.store: dict[str, object] = {}

    async def exists(self, key):
        return key in self.store

    async def get(self, key):
        v = self.store.get(key)
        return str(v) if v is not None else None

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None  # NX 抢占失败：key 已存在
        self.store[key] = value
        return True

    async def incr(self, key):
        cur = self.store.get(key, 0)
        if isinstance(cur, str):
            cur = int(cur)
        cur += 1
        self.store[key] = cur
        return cur

    async def decr(self, key):
        cur = self.store.get(key, 0)
        if isinstance(cur, str):
            cur = int(cur)
        cur -= 1
        self.store[key] = cur
        return cur

    async def expire(self, key, seconds):
        return True

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        return n


class _FakeMailer:
    """记录发送的 (email, code)，不真发邮件。"""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, email, code):
        self.sent.append((email, code))


@pytest.fixture
def client(monkeypatch):
    store = _FakeStore()
    redis = _FakeRedis()
    mailer = _FakeMailer()
    monkeypatch.setattr(auth_routes, "get_conn", store.get_conn)
    monkeypatch.setattr(auth_routes, "apply_migration", store.apply_migration)
    monkeypatch.setattr(auth_routes, "get_user_by_email", store.get_user_by_email)
    monkeypatch.setattr(auth_routes, "create_user", store.create_user)
    monkeypatch.setattr(auth_routes, "send_verification_email", mailer.send)
    monkeypatch.setattr(verification, "get_redis_coordination_client", lambda: redis)
    # 测试用 HMAC key + 缩短 PoW 前缀，走真实 ALTCHA 校验链路但瞬时求解。
    monkeypatch.setitem(settings.ALTCHA_CONFIG, "hmac_key", TEST_HMAC_KEY)
    monkeypatch.setattr(altcha_mod, "_KEY_PREFIX", "00")

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(create_auth_router())
    return TestClient(app), store, redis, mailer


def _make_payload():
    """用当前测试配置生成一个可用的 ALTCHA base64 payload（真实求解）。"""
    challenge = Challenge.from_dict(auth_routes.create_altcha_challenge())
    solution = solve_challenge(challenge, timeout=30)
    assert solution is not None
    return Payload(challenge, solution).to_base64()


def _expired_payload():
    """生成一个签名正确但已过期的 payload。"""
    challenge = create_challenge(
        algorithm="SHA-256",
        cost=1,
        key_prefix="00",
        expires_at=int(time.time()) - 60,
        hmac_secret=TEST_HMAC_KEY,
    )
    solution = solve_challenge(challenge, timeout=30)
    assert solution is not None
    return Payload(challenge, solution).to_base64()


def _send_code(c, email, payload=None):
    if payload is None:
        payload = _make_payload()
    return c.post(
        "/auth/send-verification-code",
        json={"email": email, "altcha": payload},
    )


def _register(c, email, password, code):
    return c.post("/auth/register", json={"email": email, "password": password, "code": code})


# ---------------------------------------------------------------------------
# altcha-challenge
# ---------------------------------------------------------------------------

def test_altcha_challenge_endpoint_returns_signed_challenge(client):
    c, _, _, _ = client
    r = c.get("/auth/altcha-challenge")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"parameters", "signature"}
    params = body["parameters"]
    assert params["algorithm"] == "SHA-256"
    assert params["cost"] == 1
    assert params["keyPrefix"] == "00"  # 测试内缩短的前缀
    assert params["keyLength"] == 32
    assert params["nonce"] and params["salt"]
    assert params["expiresAt"] > int(time.time())


# ---------------------------------------------------------------------------
# send-verification-code
# ---------------------------------------------------------------------------

def test_send_code_returns_200_and_emails_six_digit_code(client):
    c, _, redis, mailer = client
    r = _send_code(c, "alice@example.com")
    assert r.status_code == 200
    assert r.json()["sent"] is True
    assert len(mailer.sent) == 1
    email, code = mailer.sent[0]
    assert email == "alice@example.com"
    assert len(code) == 6 and code.isdigit()
    assert redis.store[verification._code_key("alice@example.com")] == code


def test_send_code_already_registered_returns_200_without_sending(client):
    c, store, _, mailer = client
    store.by_email["bob@example.com"] = User(
        id=1, email="bob@example.com", password_hash="x", created_at="2026-01-01T00:00:00+00:00"
    )
    r = _send_code(c, "bob@example.com")
    assert r.status_code == 200
    assert mailer.sent == []  # 防枚举：已注册邮箱不真发、不回错


def test_send_code_cooldown_returns_429(client):
    c, _, _, _ = client
    assert _send_code(c, "carol@example.com").status_code == 200
    r = _send_code(c, "carol@example.com")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "TOO_MANY_REQUESTS"


def test_send_code_daily_limit_returns_429(client, monkeypatch):
    c, _, redis, _ = client
    monkeypatch.setitem(verification.AUTH_CONFIG, "verification_code_daily_limit", 1)
    # 预置当日计数已达上限，触发 daily_limit 分支（IP 与冷却检查先行通过）。
    redis.store[verification._daily_key("dave@example.com")] = 1
    r = _send_code(c, "dave@example.com")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "TOO_MANY_REQUESTS"


def test_send_code_ip_rate_limit_returns_429_and_rolls_back(client, monkeypatch):
    c, _, redis, mailer = client
    # 固定时间戳保证窗口 key 可预期，避免跨分钟边界的偶发失效。
    monkeypatch.setattr(verification.time, "time", lambda: 1234567890.0)
    ip_key = verification._ip_window_key("testclient", 60)
    redis.store[ip_key] = 5  # 预置同 IP 本窗口已发满 5 次
    r = _send_code(c, "erin@example.com")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    assert redis.store[ip_key] == 5  # 超限计数已回滚
    assert mailer.sent == []


def test_send_code_without_altcha_returns_400_before_rate_limit(client):
    c, _, _, mailer = client
    r = c.post("/auth/send-verification-code", json={"email": "frank@example.com"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ALTCHA_REQUIRED"
    assert mailer.sent == []


def test_send_code_garbage_altcha_returns_400_and_no_email(client):
    c, _, redis, mailer = client
    r = _send_code(c, "grace@example.com", payload="not-a-payload")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ALTCHA"
    assert mailer.sent == []  # ALTCHA 不过绝不发邮件
    assert verification._code_key("grace@example.com") not in redis.store  # 也不落码


def test_send_code_tampered_altcha_returns_400(client):
    c, _, _, mailer = client
    payload = _make_payload()
    tampered = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    r = _send_code(c, "grace@example.com", payload=tampered)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ALTCHA"
    assert mailer.sent == []


def test_send_code_expired_altcha_returns_400(client):
    c, _, _, mailer = client
    r = _send_code(c, "grace@example.com", payload=_expired_payload())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ALTCHA"
    assert mailer.sent == []


def test_send_code_altcha_runs_before_rate_limit(client):
    c, _, _, _ = client
    assert _send_code(c, "henry@example.com").status_code == 200  # 首次发送成功，冷却生效
    # 冷却中 + 无效 payload：应报人机验证错误（ALTCHA 在限流之前），而非 429。
    r = _send_code(c, "henry@example.com", payload="not-a-payload")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ALTCHA"


def test_send_code_altcha_not_configured_returns_500(client, monkeypatch):
    c, _, _, mailer = client
    monkeypatch.setitem(settings.ALTCHA_CONFIG, "hmac_key", None)
    # 显式传 payload：key 缺失时无法生成合法 payload（_make_payload 同样会失败）。
    r = _send_code(c, "iris@example.com", payload="whatever")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "ALTCHA_NOT_CONFIGURED"
    assert mailer.sent == []  # 未配置 fail-closed，绝不发邮件


# ---------------------------------------------------------------------------
# register（第二步）
# ---------------------------------------------------------------------------

def test_register_missing_code_returns_400(client):
    c, _, _, _ = client
    r = c.post("/auth/register", json={"email": "alice@example.com", "password": "supersecret-123"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_REQUEST"


def test_register_wrong_code_returns_400(client):
    c, _, _, mailer = client
    _send_code(c, "alice@example.com")
    _, real_code = mailer.sent[-1]
    wrong = "999999" if real_code != "999999" else "000000"
    r = _register(c, "alice@example.com", "supersecret-123", wrong)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_VERIFICATION_CODE"


def test_register_valid_code_returns_201(client):
    c, store, _, mailer = client
    _send_code(c, "alice@example.com")
    _, code = mailer.sent[-1]
    r = _register(c, "alice@example.com", "supersecret-123", code)
    assert r.status_code == 201
    assert r.json()["email"] == "alice@example.com"
    assert "alice@example.com" in store.by_email


def test_register_attempts_exceeded_returns_400(client):
    c, _, _, mailer = client
    _send_code(c, "alice@example.com")
    _, real_code = mailer.sent[-1]
    wrong = "999999" if real_code != "999999" else "000000"
    for _ in range(5):
        assert _register(c, "alice@example.com", "supersecret-123", wrong).status_code == 400
    r = _register(c, "alice@example.com", "supersecret-123", wrong)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VERIFICATION_ATTEMPTS_EXCEEDED"
