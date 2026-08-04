# Hommey TravelAgent

Hommey 是一个面向企业差旅规划、制度问答和合规检查的智能 Agent，包含 FastAPI Web、声明式 Skill 平台、多智能体编排、当前出差任务、记忆系统和 RAG 知识库。项目只维护 Docker Web 运行入口，浏览器前端通过 HTTP/SSE 与 FastAPI 后端通信。

## 当前状态

- Web UI: `http://127.0.0.1:8000`
- 鉴权: 邮箱 + 密码登录，JWT access/refresh token
- 数据库: Docker PostgreSQL，服务名 `hommey-postgres`
- 缓存: Docker Redis，服务名 `hommey-redis`
- RAG Embedding: 默认使用 SiliconFlow 云端 `BAAI/bge-m3`，不在 Docker 镜像中部署本地 BGE/PyTorch
- 开发模式: 使用 `docker/docker-compose.dev.yml` 挂载当前源码到容器 `/app`
- Skill 管理: 管理员访问 `http://127.0.0.1:8000/admin/skills`

## 快速启动

开发时请同时使用 base compose 和 dev override：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d
```

生产运行链路只有一条：

```text
Docker Compose -> Dockerfile CMD -> Uvicorn -> webui_new.server:app
```

不再提供 CLI、独立 MCP Server 或本地 Web 启动脚本。

查看服务状态：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml ps
```

查看 Web 服务日志：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml logs -f hommey
```

改完代码后通常只需要重启 Web 服务：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml restart hommey
```

停止服务：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml down
```

## 配置

项目根目录的 `.env` 是主要配置入口，`settings.py` 会读取这些环境变量。

最少需要关注：

```bash
HOMMEY_API_KEY=your-api-key
HOMMEY_MODEL_NAME=deepseek-v4-flash
HOMMEY_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

HOMMEY_JWT_SECRET=replace-with-a-long-random-secret
HOMMEY_ADMIN_EMAILS=admin@example.com
PG_PASSWORD=replace-with-a-postgres-password

HOMMEY_EMBEDDING_API_KEY=your-siliconflow-api-key
HOMMEY_RAG_EMBEDDING_BACKEND=siliconflow
HOMMEY_EMBEDDING_MODEL=BAAI/bge-m3
```

Docker Compose 会在容器内覆盖数据库和缓存相关地址：

```bash
HOMMEY_SHORT_TERM_BACKEND=redis
HOMMEY_REDIS_HOST=hommey-redis
HOMMEY_LONG_TERM_BACKEND=postgres
HOMMEY_POSTGRES_DSN=postgresql://hommey:${PG_PASSWORD}@hommey-postgres:5432/hommey
HOMMEY_EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
HOMMEY_EMBEDDING_DIMENSION=1024
```

所以在 Docker 环境里不要把 PostgreSQL 地址写成 `localhost`。`localhost` 指的是容器自己，不是 PostgreSQL 容器。

### RAG Embedding

默认配置使用 SiliconFlow 云端 BGE：

```bash
HOMMEY_RAG_EMBEDDING_BACKEND=siliconflow
HOMMEY_EMBEDDING_MODEL=BAAI/bge-m3
HOMMEY_EMBEDDING_API_KEY=your-siliconflow-api-key
HOMMEY_EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
HOMMEY_EMBEDDING_DIMENSION=1024
```

这样 Docker 镜像不再安装 `torch` / `sentence-transformers`，也不需要挂载 `data/models/bge-small-zh-v1.5`。如果确实要回退本地模型，需要手动安装 `sentence-transformers`，并配置：

```bash
HOMMEY_RAG_EMBEDDING_BACKEND=local
HOMMEY_EMBEDDING_MODEL=data/models/bge-small-zh-v1.5
```

## 创建用户

当前前端没有注册页，但后端已有注册接口。先创建用户，再去网页登录。

```bash
curl -X POST http://127.0.0.1:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"test@example.com","password":"password123"}'
```

成功返回：

```json
{"id":1,"email":"test@example.com"}
```

然后打开：

```text
http://127.0.0.1:8000
```

用刚创建的邮箱和密码登录。

如需创建 Skill 管理员，请先在 `.env` 的 `HOMMEY_ADMIN_EMAILS` 中配置邮箱，再使用该邮箱注册。已有同邮箱用户会在 PostgreSQL 启动迁移时提升为管理员。

## Skill 平台

每个运行时 Skill 位于 `.agents/skills/<skill-name>/`：

```text
SKILL.md             标准入口：name/description frontmatter + 工作流程
hommey.yaml          可选平台扩展：版本、意图、工具、依赖和执行计划
script/agent.py      AgentScope 执行器
schemas/             输入输出契约（需要时）
references/          按需加载的证据和流程规则（需要时）
agents/openai.yaml   Skill UI 元数据（新 Skill 推荐）
```

Skill 发现遵循标准 `SKILL.md` frontmatter；`hommey.yaml` 只承载 Hommey 专属的 Agent 映射、执行顺序和治理配置。行程规划会先收集出发地、目的地、日期、时长和出差目的；信息完整后才并行查询制度、天气与公开交通信息。管理员页面支持查看 Skill 详情、启用/停用、依赖图和脱敏执行轨迹。完整设计见 [Skill 系统文档](docs/skill-system.md)。

## 鉴权流程

1. 前端登录页提交：

```http
POST /auth/login
Content-Type: application/json

{"email":"test@example.com","password":"password123"}
```

2. 后端返回：

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "bearer"
}
```

3. 前端从 `access_token` 的 JWT `sub` 里读出真实用户 id，并跳转：

```text
/chat/{user_id}
```

4. 聊天页访问个人接口时带上：

```http
Authorization: Bearer <access_token>
```

5. 后端会校验：

- token 是否存在、有效、未过期
- token 类型是否为 `access`
- token 中的 `sub` 是否能查到数据库用户
- URL 里的 `{user_id}` 是否等于当前登录用户 id

不满足会返回 401 或 403。

## 常用检查

健康检查：

```bash
curl http://127.0.0.1:8000/healthz
```

确认 Docker 容器能看到当前源码：

```bash
docker inspect hommey-app --format '{{json .Mounts}}'
```

确认容器内依赖：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml exec hommey \
  python -c "import passlib, bcrypt, jwt, email_validator, psycopg; print('ok')"
```

进入 PostgreSQL：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml exec postgres \
  psql -U hommey -d hommey
```

查询用户：

```sql
SELECT id, email, created_at FROM users ORDER BY id DESC LIMIT 10;
```

## 重新构建镜像

如果改了依赖文件，重建 Web 镜像：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml build hommey
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d hommey
```

如果构建阶段 apt 源访问失败，可以临时指定 Debian 官方源：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml build \
  --build-arg APT_MIRROR=http://deb.debian.org/debian \
  --build-arg APT_SECURITY_MIRROR=http://deb.debian.org/debian-security \
  hommey
```

## 测试

常用测试：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml exec hommey pytest
```

鉴权相关测试：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml exec hommey \
  pytest tests/test_auth_routes.py tests/test_auth_deps.py
```

注意：鉴权测试会跑真实 bcrypt，可能比普通单元测试慢。

## 项目结构

```text
webui_new/
  server.py              FastAPI 应用入口
  routes/                页面、鉴权、用户、聊天、onboarding 路由
  auth/                  用户存储、密码哈希、JWT、鉴权依赖
  skill_platform/        Skill 管理服务
  templates/             登录页、聊天页和管理员 Skill 页面
  static/                前端脚本和静态资源

agents/                  意图识别与多智能体编排
.agents/skills/          声明式 Skill 包（Codex 自动发现）
core/skill_definition.py 标准 Skill 元数据与 Hommey 扩展契约
core/skill_store.py      Skill 启停和执行轨迹存储
context/                 短期记忆和长期记忆
rag/                     RAG 文档处理、向量检索
docker/                  Dockerfile 和 compose 配置
tests/                   单元测试和契约测试
```

## 排障

### 页面还是旧的用户 ID 登录

通常是没有使用 dev override，容器跑的是旧镜像里的代码。重新执行：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d --force-recreate hommey
```

### 注册返回 500

查看日志：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml logs --tail=120 hommey
```

常见原因：

- `HOMMEY_JWT_SECRET` 未配置
- PostgreSQL 未启动或不健康
- 镜像缺少鉴权依赖，需要 rebuild

### curl 命令换行失败

反斜杠后面不要加空格：

```bash
curl -X POST http://127.0.0.1:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"test@example.com","password":"password123"}'
```
