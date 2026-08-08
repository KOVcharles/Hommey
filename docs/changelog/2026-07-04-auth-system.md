# 2026-07-04 鉴权系统（Auth System v1.0）

## Summary

为 Hommey 的 Web 层（`webui_new/`）接入**真实鉴权**：邮箱+密码注册/登录 +
JWT（access / refresh 双 token）+ 路由级「身份绑定」保护。此前 `routes/auth.py` 只有一个
收集 `user_id` 的 `/login`，任何人都能冒充任意 `user_id` 访问他人会话与数据——本版彻底闭环。

- **结果**：新增用户存储、密码哈希、JWT 签发/刷新、路由保护和前端登录接入；覆盖鉴权安全原语、存储层、路由和依赖的单元测试。

---

## 背景：为什么这么做

`webui_new/routes/chat.py` / `users.py` / `onboarding.py` 的所有业务接口都以路径参数
`{user_id}` 为 key（`manager.get(user_id)`），而这个 `user_id` 是前端自填字符串——这是
「任何人可冒充任意 user_id」的根因。本版引入真实账号体系，并把 `{user_id}` 绑定到 JWT 身份，
从机制上消除横向越权（IDOR）。

## 整体架构

```
┌──────────────────────── webui_new/auth/（鉴权子包）────────────────────────┐
│  security.py   bcrypt 哈希 / 校验  +  JWT 签发(access/refresh) / 解码        │
│  storage.py    psycopg 短连接 + 幂等建表 + User + create/get_by_email/id    │
│  deps.py       oauth2_scheme + get_current_user + require_path_user        │
│  migrations/0001_users.sql   users 表 DDL                                  │
└────────────────────────────────────────────────────────────────────────────┘
        ▲                        ▲                          ▲
        │ 直接调用                │ Depends                  │ Depends(require_path_user)
┌───────┴─────────┐    ┌──────────┴───────────┐    ┌─────────┴──────────────────┐
│ routes/auth.py  │    │ routes/chat.py       │    │ routes/users.py            │
│ /auth/register  │    │ /api/{uid}/chat      │    │ /api/{uid}/init|status|... │
│ /auth/login     │    │ /api/{uid}/chat/stream│   │ routes/onboarding.py       │
│ /auth/refresh   │    │ （仅注入依赖，函数体不变） │ /api/{uid}/onboarding[/...]│
│ /login(弃用)    │    └──────────────────────┘    └────────────────────────────┘
└─────────────────┘
```

**数据流**：

| 流程 | 链路 |
|------|------|
| 注册 | `POST /auth/register` → `RegisterRequest(EmailStr)` → bcrypt 哈希 → `create_user` → **201** `{id,email}` |
| 登录 | `POST /auth/login` → `get_user_by_email` → `verify_password`(常量时间) → 签发 access+refresh → **200** `{access_token, refresh_token, token_type}` |
| 受保护接口 | 任意受保护端点 → `require_path_user` → `get_current_user` 解码 access(type==access) 查库 → 校验 `path user_id == current_user.id` → 注入 User |
| 刷新 | `POST /auth/refresh` → `decode(refresh, type==refresh)` → 重签 access → **200**（refresh 不轮换） |

---

## 模块详解

### `webui_new/auth/security.py` — 密码 + JWT
- `hash_password` / `verify_password`：passlib bcrypt，**cost=12**；校验失败返回 `False` 不抛错（常量时间上层逻辑）。
- `create_access_token(sub)` / `create_refresh_token(sub)`：PyJWT，HS256；claims = `sub`(DB id) / `exp` / `iat` / `type`。
- `_require_secret()`：签发**与**校验前都调用；`HOMMEY_JWT_SECRET` 缺失（None/空）即抛 `ConfigError`（500），**绝不硬编码默认**。
- `AUTH_CONFIG` 每次调用读取（不缓存），便于测试 monkeypatch。

### `webui_new/auth/storage.py` — 用户存储（原生 psycopg，无 ORM）
- `User(id, email, password_hash, created_at)` dataclass；`password_hash` 仅用于登录校验，**绝不外泄/落日志**。
- `get_conn()`：复用 `MEMORY_CONFIG.long_term.postgres_dsn`（即 `HOMMEY_POSTGRES_DSN`），短连接、`autocommit + dict_row`；DSN 缺失抛 `ConfigError`。psycopg 惰性导入（无 psycopg 也可 import 模块，测试用假连接）。
- `apply_migration(conn)`：幂等 `CREATE TABLE IF NOT EXISTS users (...)`。
- `create_user` / `get_user_by_email` / `get_user_by_id`：全部参数化 `%s`，防 SQL 注入。

### `webui_new/auth/deps.py` — 依赖（鉴权 + 身份一致性）
- `oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)`：自己控制 401 文案。
- `get_current_user(token)`：无 token / 解码失败 / type 错 / sub 缺失 / 用户不存在 → 统一 **401 `UNAUTHORIZED`**，文案不区分原因（防邮箱/凭据枚举）。
- `require_path_user(user_id, current_user)`：在 `get_current_user` 之上叠加身份一致性——`path user_id != current_user.id` → **403 `FORBIDDEN`**。这是受保护端点的**唯一**注入点。

### `webui_new/routes/auth.py` — 鉴权入口
| 端点 | 行为 |
|------|------|
| `POST /auth/register` | 201 `{id,email}`；重复邮箱 409 `EMAIL_ALREADY_EXISTS`；email 非法/密码 <8 → 422。注册时幂等建表。 |
| `POST /auth/login` | 200 双 token；**常量时间**（用户不存在也对固定假哈希跑一次 bcrypt verify，防邮箱枚举）；失败 401。 |
| `POST /auth/refresh` | 200 重签 access（refresh 不轮换，回传同一 refresh）；非 refresh type / 过期 / 畸形 → 401。 |
| `POST /login` | **[deprecated]** 旧 user_id 直跳转入口，保留以兼容前端/既有路由注册测试。方案 B 下其驱动的 API 调用会自然失败，迫使前端迁移到 `/auth/login`。 |

### 路由保护注入（chat / users / onboarding）
对全部 8 个以 `{user_id}` 访问个人数据的端点，仅在 handler 签名注入 `current_user: User = Depends(require_path_user)`，**函数体零改动**：

- `chat.py`：`/api/{user_id}/chat`、`/api/{user_id}/chat/stream`
- `users.py`：`/init`、`/status`、`/is-new`、`/summary`
- `onboarding.py`：`/api/{user_id}/onboarding`、`/api/{user_id}/onboarding/preference`

效果：未登录 → 401；已登录用户 A 访问 `/api/{B}/...` → 403；本人 → 200。

---

## 安全设计要点

1. **密码**：bcrypt（cost=12）哈希；明文不入库、不入日志、不进异常 message、不进响应。
2. **常量时间登录**：用户不存在时也对固定假哈希跑一次 `verify_password`，使「邮箱不存在」与「密码错误」耗时一致。
3. **JWT secret 守护**：`HOMMEY_JWT_SECRET` 缺失时签发/校验前抛 `ConfigError`，绝不使用默认值。
4. **token 类型隔离**：access 与 refresh 用 `type` claim 区分，refresh 不能当 access 用（反之亦然），`/auth/refresh` 只接受 `type==refresh`。
5. **IDOR 防护（方案 B）**：`require_path_user` 强制 path 身份 == 认证身份，从机制上消除横向越权。
6. **401 文案不区分原因**：防响应内容/耗时被用于枚举有效邮箱。
7. **统一错误响应**：沿用 `webui_new/core/errors.py` 契约（`{success:false, error:{code,message,details,request_id}}`）。

---

## 配置（`settings.py` 新增 `AUTH_CONFIG`）

| 环境变量 | 默认 | 说明 |
|---------|------|------|
| `HOMMEY_JWT_SECRET` | —（必填） | JWT 签名密钥；缺失即报错。 |
| `HOMMEY_POSTGRES_DSN` | —（必填） | PG 连接串；复用长期记忆同一个。Docker 内使用 `hommey-postgres:5432`，宿主机连接使用映射端口。 |
| `HOMMEY_JWT_ALGO` | `HS256` | JWT 算法 |
| `HOMMEY_JWT_ACCESS_EXPIRE_MINUTES` | `30` | access token 有效期 |
| `HOMMEY_JWT_REFRESH_EXPIRE_DAYS` | `7` | refresh token 有效期 |

新增依赖（已写入 `requirements.txt`）：
`PyJWT>=2.8`、`passlib[bcrypt]>=1.7.4`、`bcrypt>=4.0,<4.1`、`email-validator>=2.0`、`psycopg[binary]`。

---

## 测试

| 文件 | 覆盖 |
|------|------|
| `tests/test_auth_security.py` | bcrypt 哈希/校验、JWT claims(access/refresh/type/exp)、签名篡改、过期、secret 缺失守护（纯单元，无 DB） |
| `tests/test_auth_storage.py` | 建表 DDL、create/get_by_email/get_by_id（含 `password_hash` 契约），用 RecordingConnection 风格假连接 |
| `tests/test_auth_routes.py` | `/auth/register`(201/409/422)、`/auth/login`(200/401/防枚举文案一致)、`/auth/refresh`(200/401/类型隔离)；走真实 bcrypt+JWT + fake 存储 |
| `tests/test_auth_deps.py` | 401 矩阵（无 token/篡改/过期/refresh 当 access/用户不存在）、**403 跨用户**、200 本人；走真实 token 解码 + fake 存储 |

> 既有 `test_webui_error_responses.py` / `test_observability.py` 中聚焦「业务错误契约」的用例，
> 通过 `app.dependency_overrides[require_path_user]` 绕过鉴权直达业务逻辑（鉴权本身由
> `test_auth_deps.py` 单独覆盖），避免重复造 token。

真实链路冒烟（建表 → 注册 → 查邮箱 → bcrypt 校验 → 签发/解码 JWT，连真实 PG）已通过：
`bcrypt $2b$12$ 哈希 / access ttl=1800s / sub=user_id`。

---

## 已知范围与后续

**v1.0 已交付**：注册 / 登录 / JWT(access+refresh) / `get_current_user` + `require_path_user` / 8 个端点身份绑定保护。

**v1.0 不含（留作后续）**：
- OAuth2 / 第三方登录（OIDC）
- RBAC 角色权限
- 邮箱验证 / 找回密码 / 改密
- refresh token 轮换与 Redis 吊销列表（主动登出）

**前端迁移提示**：`/auth/login` 返回的 access token（JWT）第二段 base64url 解码即得 `sub` = 本人 id，
无需 secret/验签即可读取，用于拼接 `/chat/{id}` 等路径。旧 `/login` 已标 deprecated。

---

## 2026-07-05 更新：前端接入、Docker 开发流与本地排障

### 前端已接入 JWT 鉴权

Web 登录页已从旧的「输入任意 `user_id` → `POST /login` → `/chat/{user_id}`」迁移为真实登录：

1. 登录页输入邮箱和密码。
2. 前端请求 `POST /auth/login`。
3. 成功后把 `access_token` / `refresh_token` 写入 `localStorage`。
4. 前端从 access token 的 JWT `sub` 读取 canonical user id。
5. 跳转到 `/chat/{sub}`。
6. 聊天页所有 `/api/{user_id}/...` 请求统一带：

```http
Authorization: Bearer <access_token>
```

access token 过期时，前端会调用 `/auth/refresh` 换新 access token 并重试一次；刷新失败则清理本地 token，并在初始化遮罩上提供返回登录页入口。

涉及文件：

- `webui_new/templates/login.html`
- `webui_new/static/app.js`

### 当前推荐 Docker 启动方式

开发环境请同时使用 base compose 和 dev override：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d
```

原因：

- `docker/docker-compose.yml` 定义 app / postgres / redis。
- `docker/docker-compose.dev.yml` 会把当前源码目录挂载到容器 `/app`。
- 只用 base compose 时，可能跑到镜像里的旧代码，页面仍显示旧的用户 ID 登录框。

改完代码通常只需：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml restart hommey
```

当前 Docker 服务：

| 服务 | 说明 |
|------|------|
| `hommey-app` | FastAPI Web 服务，对外端口 `8000` |
| `hommey-postgres` | PostgreSQL 16，容器内主机名 `hommey-postgres`，宿主机端口 `5432` |
| `hommey-redis` | Redis 7 |

### Docker PostgreSQL 与账号存储

`POST /auth/register` 创建的账号写入 Docker PostgreSQL：

| 项 | 值 |
|----|----|
| 容器 | `hommey-postgres` |
| 数据库 | `hommey` |
| 表 | `users` |
| 持久化 | Docker volume（删除容器不丢，`docker compose down -v` 会删除） |

账号表结构：

```sql
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL    PRIMARY KEY,
    email         TEXT         UNIQUE NOT NULL,
    password_hash TEXT         NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
```

密码只保存 bcrypt 哈希，不保存明文。

创建用户：

```bash
curl -X POST http://127.0.0.1:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"test@example.com","password":"password123"}'
```

查看账号：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml exec postgres \
  psql -U hommey -d hommey \
  -c "SELECT id, email, created_at FROM users ORDER BY id DESC;"
```

### 当前数据库内容概况

当前 `hommey` 数据库包含 5 张表：

| 表 | 用途 |
|----|------|
| `users` | 新鉴权账号，`id` 是 JWT `sub` 使用的 canonical user id |
| `user_preferences` | 用户偏好 |
| `trip_history` | 历史行程 |
| `chat_history` | 聊天历史 |
| `user_statistics` | 用户统计 |

注意：旧业务记忆表使用 `text user_id`，历史测试数据目前可能仍挂在旧字符串用户（例如 `test`）名下；新鉴权账号使用数字 id（例如 `5`）。因此旧记忆数据不会自动出现在新账号下，后续如需保留旧数据，需要做 user_id 迁移或映射。

### VS Code SQLTools 连接参数

在本机 VS Code / SQLTools 连接 Docker PostgreSQL 时使用宿主机映射端口：

```text
Connection name: Hommey PostgreSQL
Driver: PostgreSQL
Server / Host: 127.0.0.1
Port: 5432
Database: hommey
Username: hommey
Password: <postgres-password>
SSL: Disabled
```

如果写入 `.vscode/settings.json`：

```json
{
  "sqltools.connections": [
    {
      "name": "Hommey PostgreSQL",
      "driver": "PostgreSQL",
      "server": "127.0.0.1",
      "port": 5432,
      "database": "hommey",
      "username": "hommey",
      "password": "<postgres-password>",
      "connectionTimeout": 30
    }
  ]
}
```

区分两种地址：

- VS Code / 宿主机连接：`127.0.0.1:5432`
- Docker 容器内部连接：`hommey-postgres:5432`

### 排障记录

- 本地非 Docker 服务曾因当前 Python 环境缺少 `psycopg` 导致 `/auth/register` 500；Docker 方式不应依赖宿主机 Python 包。
- 旧 Docker 镜像缺少 `passlib` / `bcrypt` / `email-validator` 等新鉴权依赖，重启后会在 import `webui_new.auth.security` 时失败。长期解法是重建镜像；开发时确认 `requirements.txt` 已包含这些依赖。
- 如果页面仍是旧版用户 ID 登录框，通常是没有使用 `docker-compose.dev.yml` 挂载当前源码。
