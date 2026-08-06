<p align="center">
  <img src="webui_new/static/brand/hommey-mark.svg" width="120" alt="Hommey">
</p>

<h1 align="center">Hommey 商旅助手</h1>

<p align="center"><b>把复杂差旅，变成清楚的一程</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB" alt="Python 3.11">
  <img src="https://img.shields.io/badge/FastAPI-0.115-009688" alt="FastAPI">
  <img src="https://img.shields.io/badge/AgentScope-1.0.16-6B5BFF" alt="AgentScope">
  <img src="https://img.shields.io/badge/PostgreSQL-16-336791" alt="PostgreSQL 16">
  <img src="https://img.shields.io/badge/Redis-7-DC382D" alt="Redis 7">
  <img src="https://img.shields.io/badge/流式聊天-NDJSON-ff6d4a" alt="NDJSON 流式聊天">
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED" alt="Docker Compose">
</p>

> **Hommey** 是面向企业差旅场景的智能 Agent:**行程规划 · 制度问答 · 合规检查** 三合一。
> 基于可治理的声明式 Skill 平台与双层记忆系统,由多智能体并行编排、混合检索 RAG 全程证据溯源,
> 让每一次出差从规划到报销都有据可依。

---

## 目录

- [✨ 特性速览](#特性速览)
- [🏗 架构总览](#架构总览)
- [🧭 核心能力](#核心能力)
- [🧩 Skill 生态](#skill-生态)
- [🛠 技术栈](#技术栈)
- [🚀 快速开始](#快速开始)
- [⚙️ 配置](#配置)
- [🔐 鉴权流程](#鉴权流程)
- [📁 项目结构](#项目结构)
- [🧪 测试](#测试)
- [📚 文档](#文档)
- [🛠 运维与排障](#运维与排障)

---

## ✨ 特性速览

| | | |
|---|---|---|
| 🧠 **多智能体并行编排** | 意图识别 → 任务分解 → 校验 → DAG 依赖图 → 同优先级 `asyncio.gather` 并行执行,最终合成结构化答案卡片 | 
| 🎯 **声明式 Skill 平台** | 9 个 Git 管理的业务能力包,`SKILL.md` 标准入口 + `hommey.yaml` 平台扩展;运行时启停、依赖图、执行轨迹全可观测 | 
| 🧠 **双层记忆系统** | 短期记忆(Redis 滑动窗口)支撑多轮上下文;长期记忆(PostgreSQL)沉淀偏好、历史与当前出差任务,跨会话注入 | 
| 📚 **混合检索 RAG** | 向量检索 + BM25 + RRF 融合,企业制度基于证据回答,无证据时明确拒答、绝不编造 | 
| ⚡ **实时流式聊天** | NDJSON 流式渲染,实时展示正在工作的 Agent 标签与进度,结果以日程时间线、天气卡片、出行清单等结构化卡片呈现 | 
| 🗺 **对话式行程收集** | 5 项必填出差信息逐项收集,可一键补全、冲突澄清,信息完备后自动续跑完整规划链路 | 
| 🔐 **企业级鉴权** | JWT access/refresh、bcrypt 恒定时间登录、基于路径的用户身份隔离(IDOR 防护)、管理员角色 | 
| 🛡 **弹性工程底座** | Redis Lua 原子分布式锁 + 全局并发信号量、熔断器、执行预算、幂等重放、请求超时与客户端断连取消 | 

## 现状速览

| 项目 | 说明 |
| --- | --- |
| Web UI | `http://127.0.0.1:8000`,登录页自带注册 |
| 鉴权 | 邮箱 + 密码,JWT access(30 min)/ refresh(7 d) |
| 数据库 | Docker PostgreSQL,服务名 `hommey-postgres` |
| 缓存 | Docker Redis,服务名 `hommey-redis` |
| RAG Embedding | 默认 SiliconFlow 云端 `BAAI/bge-m3`,镜像不内置本地 PyTorch |
| 开发模式 | `docker-compose.dev.yml` 挂载当前源码到容器 `/app` |
| Skill 管理 | 管理员访问 `http://127.0.0.1:8000/admin/skills` |

---

## 🏗 架构总览

```mermaid
flowchart LR
    U[用户请求<br/>NDJSON 流式] --> G{领域门控}
    G -- 拒绝 --> R[拒答边界]
    G --> C[能力目录<br/>SKILL.md 发现]
    C --> I[意图识别<br/>规则路由 + LLM 置信度]
    I --> T[任务分解 → 校验]
    T --> D[DAG 依赖图]
    D --> E[并行执行<br/>asyncio.gather]
    E --> RAG[(混合检索 RAG<br/>向量 + BM25)]
    E --> MEM[(双层记忆<br/>短期 Redis · 长期 Postgres)]
    E --> A[答案合成<br/>结构化卡片 + 证据来源]
    A --> UI[前端渲染<br/>流式 + 富卡片]
```

核心链路一句话:**一次用户请求,经过领域门控、意图识别与任务分解,多个 Skill 智能体按依赖关系并行执行,各自基于 RAG 证据与用户记忆产出片段,最终合成一张带来源引用的结构化答案卡片**。

一次完整的差旅规划请求,会走一条编排式复合工作流:

```text
收集出差事项(event-collection)
  → 并行查询制度(ask-question) + 天气/交通(query-info)
  → 生成行程(plan-trip)
  → 合规检查(check-trip-compliance)
```

---

## 🧭 核心能力

### 🧠 多智能体编排

- **双层意图识别**:规则路由(`FastIntentRouter`)先做关键词快速匹配,模糊或依赖上下文时才交给 LLM 识别,置信度低于阈值(默认 0.65)即拒答或追问。
- **任务级编排(V2)**:`MultiIntentPipeline` 将一次多意图请求拆分为**独立语义任务**,每个任务持有自己的限定上下文——从根上消除"查天气的任务去翻制度库"这类跨意图串扰。
  `TaskDecomposer → TaskValidator → TaskGraphBuilder → TaskExecutor → AnswerComposer`,每一步都有确定性兜底,LLM 只产出语义任务、不直接选 Agent。
- **并行与失败隔离**:同优先级 Agent 通过 `asyncio.gather` 并行,高优先级结果供低优先级读取;单个任务失败不影响其他任务,失败区块在卡片中标红保留。

### 🎯 声明式 Skill 平台

Skill 是一套**受 Git 管理的业务能力包**,把一次性的 Prompt、Agent 代码与业务编排沉淀为可发现、可校验、可组合、可启停、可观测的运行时能力:

```text
.agents/skills/<skill-name>/
├── SKILL.md               # 标准入口:name/description frontmatter + 工作流程
├── hommey.yaml            # 平台扩展:版本、意图、Agent、工具、风险、依赖、执行计划
├── script/agent.py        # AgentScope 执行器(懒加载,未触发零开销)
├── schemas/               # 输入输出契约
└── references/            # 按需加载的证据与流程规则
```

- **意图 ↔ Skill 一一映射**,启动时从 Skill 定义自动生成能力目录;
- **运行时启停**:PostgreSQL 中的启停状态可覆盖默认配置,无需改代码;
- **全链路可观测**:每次执行写入脱敏轨迹(输入输出摘要、证据数、错误),管理页展示成功率与平均耗时;
- **治理边界清晰**:稳定流程沉淀为 Skill,补贴金额等易变制度放入 RAG 文档,用户偏好进记忆系统,各归其位。

完整设计见 [`docs/skill-system.md`](docs/skill-system.md)。

### 🧠 双层记忆系统

- **短期记忆**:最近对话滑动窗口(默认 10 轮),支撑指代消解与多轮上下文;Docker 下基于 Redis。
- **长期记忆**:偏好、完整聊天历史、行程历史与统计,基于 PostgreSQL(参数化查询,按用户隔离);轻量部署可回退 JSON 文件。
- **当前出差任务**:每用户一个 `active_trip` 工作区,由 `event-collection` 增量合并,跨会话注入上下文——所以一句「补贴呢?」能准确对上正在进行的行程。
- **会话摘要**:长对话按段增量生成摘要,读时惰性触发、水位推进,不阻塞主链路。
- **安全**:记忆写入前做 PII 脱敏,用户记忆以「不可信数据」包装注入 Prompt,抵御提示注入。

### 📚 混合检索 RAG

- **混合检索**:向量(top-k)+ 进程内 BM25,RRF 融合 + 关键词重排 + 领域术语查询扩展;
- **向量库**:Milvus Lite(嵌入式、文件化存储),默认云端 `BAAI/bge-m3`(1024 维)Embedding,也支持本地 `sentence-transformers` 回退;
- **证据溯源**:答案卡片携带来源引用,无证据时明确拒答。

### 🛡 弹性与并发

- **全局并发串行化**:每请求先取进程内 `asyncio.Lock` → Redis 分布式锁(按用户、跨 worker)→ 全局信号量,全部由原子 Lua 脚本实现;心跳续约 + 锁丢失即中止在途处理,保证同用户请求严格串行;
- **熔断器**:Redis 共享熔断(closed → open → half-open),上游异常自动降级;
- **执行预算**:单请求最多 8 次 Agent 调用、16 次外部调用(每类型 6 次),请求默认 240s 超时;
- **幂等重放**:请求携带 `request_id`,已记录响应直接重放,客户端重发不会重复跑 Agent;
- **流式断连取消**:客户端断开即取消孤儿任务,不留下未释放的锁与未收尾的记忆写入。

### 📡 可观测性

- `/healthz`(存活)、`/readyz`(组件化预检)、`/metrics`(Prometheus 文本指标);
- 统一错误契约 `{success, error: {code, message, details, request_id}}`,结构化日志可切 JSON;
- 告警信号同时以日志 + 指标输出(`http_5xx`、`upstream_timeout`、`circuit_open`、`db_connect_failure`)。

---

## 🧩 Skill 生态

仓库内置 9 个运行时 Skill,覆盖差旅核心链路:

| Skill | 意图 | 能力 | 状态 |
| --- | --- | --- | --- |
| `ask-question` | rag_knowledge | 基于企业 RAG 回答差旅制度,证据溯源 | 已接入 |
| `event-collection` | event_collection | 收集并增量更新当前出差事项 | 已接入 |
| `plan-trip` | itinerary_planning | 组合事项、制度与外部信息生成行程 | 已接入 |
| `check-trip-compliance` | trip_compliance | 依据制度证据检查拟定行程合规性 | 已接入 |
| `query-info` | information_query | 查询差旅天气与公开交通信息 | 已接入 |
| `memory-query` | memory_query | 查询当前用户的差旅记忆 | 已接入 |
| `preference` | preference | 保存或更新差旅偏好 | 已接入 |
| `chitchat` | chitchat | 简短礼貌交互与能力介绍 | 已接入 |
| `mcp-tool` | mcp_tool | 路由已授权的 MCP 调用 | 默认停用 |

---

## 🛠 技术栈

| 层 | 选型 |
| --- | --- |
| 语言 / Web | Python 3.11 · FastAPI · Uvicorn(多 worker) |
| 智能体框架 | AgentScope 1.0 · 自研编排流水线 · MCP 客户端 |
| 存储 | PostgreSQL 16(连接池、checksum 校验迁移)· Redis 7 |
| 检索 | Milvus Lite · BM25 · RRF 融合 · SiliconFlow bge-m3 |
| 前端 | Jinja2 模板 + 原生 JS,**零构建依赖**,手写设计令牌 CSS,明暗双主题 |
| 部署 | Docker Compose(镜像仅含 Web 运行入口) |

---

## 🚀 快速开始

### 1. 配置环境变量

复制 `.env.example` 为 `.env`,至少填写:

```bash
HOMMEY_API_KEY=your-api-key
HOMMEY_MODEL_NAME=deepseek-v3
HOMMEY_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
HOMMEY_JWT_SECRET=replace-with-a-long-random-secret
HOMMEY_ADMIN_EMAILS=admin@example.com
PG_PASSWORD=replace-with-a-postgres-password
```

### 2. 启动服务

开发时同时使用 base compose 与 dev override(挂载当前源码):

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d
```

生产运行链路只有一条:`Docker Compose → Dockerfile CMD → Uvicorn → webui_new.server:app`。不再提供 CLI、独立 MCP Server 或本地 Web 启动脚本。

### 3. 创建用户并登录

当前前端登录页自带注册,也可用 API 直接创建:

```bash
curl -X POST http://127.0.0.1:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"test@example.com","password":"password123"}'
```

打开 `http://127.0.0.1:8000`,用刚创建的邮箱和密码登录即可开始对话。

> 创建 Skill 管理员:先在 `.env` 的 `HOMMEY_ADMIN_EMAILS` 配置邮箱,再使用该邮箱注册;已有同邮箱用户会在 PostgreSQL 启动迁移时自动提升为管理员。

---

## ⚙️ 配置

项目根目录的 `.env` 是主要配置入口,`settings.py` 读取全部环境变量。Docker Compose 会在容器内覆盖数据库与缓存相关地址:

```bash
HOMMEY_SHORT_TERM_BACKEND=redis
HOMMEY_REDIS_HOST=hommey-redis
HOMMEY_LONG_TERM_BACKEND=postgres
HOMMEY_POSTGRES_DSN=postgresql://hommey:${PG_PASSWORD}@hommey-postgres:5432/hommey
```

> ⚠️ 在 Docker 环境里不要把 PostgreSQL 地址写成 `localhost`——它指的是容器自己,不是 PostgreSQL 容器。

### RAG Embedding

默认使用 SiliconFlow 云端 BGE,镜像无需安装 `torch` / `sentence-transformers`:

```bash
HOMMEY_RAG_EMBEDDING_BACKEND=siliconflow
HOMMEY_EMBEDDING_MODEL=BAAI/bge-m3
HOMMEY_EMBEDDING_API_KEY=your-siliconflow-api-key
HOMMEY_EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
HOMMEY_EMBEDDING_DIMENSION=1024
```

如需回退本地模型,需手动安装 `sentence-transformers`,并配置 `HOMMEY_RAG_EMBEDDING_BACKEND=local` 与 `HOMMEY_EMBEDDING_MODEL=data/models/bge-small-zh-v1.5`。

完整的配置项清单见 `settings.py` 与 [`docs/skill-system.md`](docs/skill-system.md)。

---

## 🔐 鉴权流程

1. 前端登录页提交 `POST /auth/login`(`{email, password}`);
2. 后端返回 `access_token` / `refresh_token`(`token_type: bearer`);
3. 前端从 `access_token` 的 JWT `sub` 读出真实用户 id,跳转 `/chat/{user_id}`;
4. 聊天页访问个人接口时携带 `Authorization: Bearer <access_token>`;
5. 后端校验:token 存在且未过期、类型为 `access`、`sub` 能查到用户、URL 中的 `{user_id}` 与当前登录用户一致,否则返回 401 / 403。

---

## 📁 项目结构

```text
webui_new/               FastAPI 应用入口(server.py)、路由、鉴权、Skill 管理、模板与静态资源
agents/                  意图识别与多智能体编排
.agents/skills/          声明式 Skill 包(标准 Agent Skills 结构)
core/                    Skill 契约与存储、编排流水线、行程收集、答案文档
context/                 短期 / 长期记忆、会话摘要
rag/                     RAG 文档处理、混合检索、向量存储
hommey_mcp/              项目自有的 MCP 客户端
docker/                  Dockerfile 与 Compose 配置
docs/                    设计文档(skill 系统、编排、记忆、错误契约等)
tests/                   单元测试与契约测试
```

---

## 🧪 测试

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml exec hommey pytest
```

鉴权相关测试会跑真实 bcrypt,可能比普通单元测试慢:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml exec hommey \
  pytest tests/test_auth_routes.py tests/test_auth_deps.py
```

---

## 📚 文档

| 文档 | 内容 |
| --- | --- |
| [Skill 系统](docs/skill-system.md) | Skill 包结构、加载校验、编排调度、治理与观测 |
| [任务编排 v2](docs/task-orchestration-v2.md) | 任务级多意图流水线的设计与演进路线 |
| [记忆系统](docs/memory-system.md) | 双层记忆、当前出差任务、会话摘要与隐私边界 |
| [行程收集体验](docs/trip-intake-experience.md) | 结构化出差信息收集的状态机与自动续跑 |
| [错误契约](docs/error-codes.md) | 统一错误码、可观测性字段与告警信号 |
| [项目结构](docs/project-structure.md) | 模块边界与目标包结构 |

---

## 🛠 运维与排障

<details>
<summary><b>常用检查</b></summary>

健康检查:

```bash
curl http://127.0.0.1:8000/healthz
```

确认 Docker 容器能看到当前源码:

```bash
docker inspect hommey-app --format '{{json .Mounts}}'
```

确认容器内依赖:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml exec hommey \
  python -c "import passlib, bcrypt, jwt, email_validator, psycopg; print('ok')"
```

进入 PostgreSQL:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml exec postgres \
  psql -U hommey -d hommey
```

查询用户:

```sql
SELECT id, email, created_at FROM users ORDER BY id DESC LIMIT 10;
```

</details>

<details>
<summary><b>重新构建镜像</b></summary>

改了依赖文件后重建 Web 镜像:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml build hommey
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d hommey
```

如果构建阶段 apt 源访问失败,可临时指定 Debian 官方源:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml build \
  --build-arg APT_MIRROR=http://deb.debian.org/debian \
  --build-arg APT_SECURITY_MIRROR=http://deb.debian.org/debian-security \
  hommey
```

</details>

<details>
<summary><b>排障</b></summary>

**页面还是旧的用户 ID 登录** —— 通常是没有使用 dev override,容器跑的是旧镜像代码:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d --force-recreate hommey
```

**注册返回 500** —— 查看日志:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml logs --tail=120 hommey
```

常见原因:`HOMMEY_JWT_SECRET` 未配置、PostgreSQL 未启动或不健康、镜像缺少鉴权依赖需要 rebuild。

**curl 命令换行失败** —— 反斜杠后面不要加空格。

</details>

---

## 📄 许可

> 待补充:项目目前未包含 LICENSE 文件。若计划开源,建议补充后在此注明许可协议。

---

*文档维护:产品功能变更后请同步更新本 README,避免出现「前端没有注册页」这类过时描述。*
