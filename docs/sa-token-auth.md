# Sa-Token 用户鉴权与配置

用户登录使用 Sa-Token 1.46.0 的随机令牌与 Redis 会话。浏览器只访问 Spring，内部 Spring/Python 调用继续使用 RS256 执行凭证。

## 迁移后的行为

- 用户密码仍使用 PostgreSQL 中的 BCrypt 哈希，用户 ID、角色和所有业务数据保持原样，无须数据库迁移。
- 登录响应保留标准字段 `access_token`，其值现在是随机字符串；不再签发用户 JWT 或 refresh token，也不再提供 `/auth/refresh`。
- 前端通过登录响应和 `/api/me` 获取身份，不解码令牌。`hommey.token` 保存新令牌，旧 `hommey.access_token` / `hommey.refresh_token` 会被清理。
- `/api/**`、`/auth/logout` 和 `/auth/logout-all` 由 Sa-Token 检查登录；管理员角色从数据库实时读取。会话、资料和行程的归属检查仍由业务服务执行。
- 用户令牌无法访问 `/internal/business/**`，内部 Agent JWT 无法用作浏览器登录令牌。内部执行凭证仍限定用户、会话、请求，只有 Spring 持有 RSA 私钥。
- Redis 键使用 `hommey:auth:session:` 前缀，避免与注册验证码、Python 缓存键混淆。Redis 故障时认证请求失败，不回退到内存会话。

部署后，已有用户需要重新登录一次；旧用户 JWT 无法转换为 Sa-Token 会话。请同时更新后端和前端资源，避免新旧协议混用。

## 配置位置

1. `backend/src/main/resources/application.yml`：Redis 连接、`sa-token` 策略及内部 JWT 配置。
2. `.env.engineering`：本机开发与 Docker Compose 使用的环境变量，示例在 `.env.engineering.example`。
3. `docker/docker-compose.engineering.yml`：将环境变量传给容器。容器内固定使用 `redis:6379`，本机使用映射端口。

Spring Boot 本身不会自动读取项目根目录的 `.env.engineering`。本机启动使用 `scripts/start-backend.ps1`，它会读取该文件；IDE 启动需使用仓库的调试配置或把变量传给进程。Docker Compose 使用 `--env-file .env.engineering`。

已有 `.env.engineering` 不会被准备脚本覆盖，可按需追加以下配置；没有配置的项使用代码中的默认值：

```dotenv
# 本机连接；Docker 容器内由 Compose 覆盖为 redis:6379
HOMMEY_REDIS_HOST=localhost
HOMMEY_REDIS_PORT=56380
HOMMEY_REDIS_DB=0
HOMMEY_REDIS_PASSWORD=

# 单位：秒
HOMMEY_AUTH_TIMEOUT_SECONDS=604800
HOMMEY_AUTH_ACTIVE_TIMEOUT_SECONDS=1800
HOMMEY_AUTH_ALLOW_CONCURRENT=true
HOMMEY_AUTH_MAX_LOGIN_COUNT=5

# 以下仅用于 Spring/Python 的内部执行凭证
HOMMEY_JWT_ISSUER=hommey-backend
HOMMEY_AGENT_TOKEN_DURATION=5m
```

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `HOMMEY_REDIS_HOST` | `localhost` | 本机 Spring 的 Redis 主机；Compose 内覆盖为 `redis` |
| `HOMMEY_REDIS_PORT` | `56380` | 本机 Redis 端口，也是 Compose 发布到宿主机的端口 |
| `HOMMEY_REDIS_DB` | `0` | Redis 数据库索引；Spring 与 Python 从 Compose 获取同一值 |
| `HOMMEY_REDIS_PASSWORD` | 空 | Redis 密码；Compose 同时配置 Redis 服务端与客户端 |
| `HOMMEY_AUTH_TIMEOUT_SECONDS` | `604800` | 登录最长有效期，7 天；到期必须重新登录 |
| `HOMMEY_AUTH_ACTIVE_TIMEOUT_SECONDS` | `1800` | 连续 30 分钟没有认证请求则登录失效；设 `-1` 关闭空闲限制 |
| `HOMMEY_AUTH_ALLOW_CONCURRENT` | `true` | 允许同时登录；设 `false` 时，同一设备类型的新登录顶掉旧登录 |
| `HOMMEY_AUTH_MAX_LOGIN_COUNT` | `5` | 最多保留 5 个登录令牌，超出后最早的登录失效；`-1` 不限 |
| `HOMMEY_AGENT_TOKEN_DURATION` | `5m` | 内部执行凭证有效期，不影响用户登录 |

`auto-renew: true` 自动更新空闲计时，不延长 7 天的最长有效期。多设备不共享令牌（`is-share: false`），退出当前设备不会退出其他设备。并发限制的数量按登录令牌计数，只有允许并发登录时才有意义。

Sa-Token 固定使用 `Authorization: Bearer <access_token>`，只读取 Header，不接受 Cookie、URL 查询参数或请求体中的令牌。前端无需配置 Sa-Token 密钥；RSA 公私钥文件仍用于内部执行凭证，不要删除。

Redis 插件要求 Redis 6.0+；工程栈已使用 Redis 7。Compose 开启 AOF 并保留数据卷，重建 Redis 容器时保留会话数据；删除 Redis 卷或登录键会使用户重新登录。AOF 默认策略在异常断电时仍可能丢失最近写入，不应将 Redis 会话当成永久业务数据。

## 本机开发

在仓库根目录执行：

```powershell
# 初次配置，已有 .env.engineering 和密钥会保留
.\.venv\Scripts\python.exe scripts/prepare_engineering.py

docker compose --env-file .env.engineering -f docker/docker-compose.engineering.yml up -d postgres redis
.\scripts\start-backend.ps1

# 另一个终端，需要 AI 功能时启动
.\.venv\Scripts\python.exe scripts/run_ai_service.py
```

使用 JDK 21。默认 Spring 地址为 `http://localhost:8080`，Redis 为 `localhost:56380`。如果使用已有的 Redis，可在环境文件中改主机、端口和密码；此时不必启动 Compose 的 Redis 服务。

## Docker 更新

用户鉴权代码和前端资源都打包在 backend 镜像中。已有工程栈只需要更新 backend 与 Redis 配置：

```powershell
docker compose --env-file .env.engineering -f docker/docker-compose.engineering.yml up -d redis
docker compose --env-file .env.engineering -f docker/docker-compose.engineering.yml up -d --build --no-deps backend
docker compose --env-file .env.engineering -f docker/docker-compose.engineering.yml ps
```

入口仍为 `http://localhost:8088/login`，本机后端为 `http://localhost:8080/login`。这次迁移没有修改 SQL/Flyway 文件，不需要清空或重建 PostgreSQL。

修改 Redis 密码、数据库索引或内部 issuer 时，Spring、Python 和 RAG worker 必须使用一致的配置。修改这些共享项时同步重建服务：

```powershell
docker compose --env-file .env.engineering -f docker/docker-compose.engineering.yml up -d --build
```

已有 `.env.engineering` 内没有新项也可以运行，Compose 会应用默认值。修改登录策略后重建 backend；策略主要影响新创建的登录，若要所有用户立刻采用新有效期，需要注销其现有会话或要求重新登录。

## HTTP 接口

登录请求仍兼容原有邮箱与密码，`device` 可选，默认 `web`；它代表设备类型标签，不代表唯一物理设备：

```http
POST /auth/login
Content-Type: application/json

{"email":"user@example.com","password":"your-password","device":"web"}
```

```json
{
  "access_token": "a-random-64-character-token",
  "token_type": "bearer",
  "expires_in": 604800,
  "user": {"id": 1, "email": "user@example.com", "role": "user"}
}
```

所有以下请求都需要 `Authorization: Bearer <access_token>`：

| 接口 | 行为 |
| --- | --- |
| `GET /api/me` | 获取当前用户 ID、邮箱和实时角色 |
| `POST /auth/logout` | 撤销当前令牌 |
| `POST /auth/logout-all` | 撤销当前账号所有设备的登录令牌 |
| `POST /api/admin/users/{userId}/kickout` | 管理员强制指定用户的全部登录下线 |

踢下线不等同于封禁账号，被踢用户仍可用正确密码重新登录。普通用户不能调用管理员接口。已有角色存储不变：`HOMMEY_ADMIN_EMAILS` 影响新注册用户的角色，不会自动修改已存在用户的角色。

未登录、过期、被踢下线返回 `401`；角色或数据归属不足返回 `403`；Redis 连接失败返回 `503 AUTH_UNAVAILABLE`，沿用项目的 JSON 错误结构和请求 ID。

聊天页的退出按钮先请求服务端注销，成功后才清理浏览器数据。网络或服务端失败会提示重试，避免界面显示已退出而令牌仍有效。前端保留 Header 令牌方式，浏览器存储方式没有改成 HttpOnly Cookie。

用户注销或踢下线阻止后续用户请求；已经开始的聊天执行和已签发的内部凭证可能继续到完成或 5 分钟过期。若要终止已开始的任务，使用现有取消执行接口；本次没有将退出登录改为取消任务。

## 验证

```powershell
node --test tests/test_auth_session.cjs tests/test_chat_stream_delivery.cjs
cd backend
.\mvnw.cmd verify
```

Java 测试使用 Testcontainers 的独立 PostgreSQL、Redis，验证真实登录、令牌撤销、多设备、最大登录数、总有效期、空闲续签、管理员权限即时变更、旧令牌拒绝、数据归属和内部凭证隔离，也保留流式聊天与业务事务测试。

Linux / CI 使用 `bash mvnw verify`。这台 Windows 的旧 JDK 21 网络问题见工程化指南，可以在 Linux Maven 容器中验证。浏览器功能检查使用真实模板与脚本配合模拟 HTTP 接口；不会发送注册邮件或模型请求。

官方参考：[Spring Boot 3 集成](https://sa-token.com/start/example.html)、[Redis 集成](https://sa-token.com/up/integ-redis.html)、[有效期](https://sa-token.com/fun/token-timeout.html)、[踢人下线](https://sa-token.com/use/kick.html)。
