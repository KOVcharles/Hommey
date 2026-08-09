# Hommey 全系统安全、意图、回复与前端体验优化报告

- 日期：2026-08-09
- 交付分支：`agent/durable-orchestration-state`
- 优化原则：保留现有 Agent → Skill → DAG 编排 → AnswerDocument 总体架构，只收紧权限边界、失败语义、可信边界和展示一致性
- 当前结论：本轮发现的严重数据安全与知识库可用性风险已修复；容器内全量测试、真实 Milvus Lite 切换、运行态健康检查和桌面/移动端视觉验收均通过

## 1. 执行摘要

本轮不是重写系统，而是在原框架上做兼容性加固。核心变化如下：

1. 知识库上传、刷新和刷新状态改为仅管理员可用，普通员工继续保留制度查阅能力。
2. 全量 RAG 刷新由“先删除旧库再写入”改为“预生成向量 → 写入临时集合 → 校验 → 蓝绿切换 → 失败回滚”，空目录、解析失败、向量服务失败均不会清空线上知识库。
3. 同名制度文件不再静默覆盖；文件先写临时文件、`fsync` 后原子落盘。
4. 修复采购审批、医疗费报销、政府补贴等非差旅问题误入差旅 RAG；修复自然偏好和个人历史问题因缺少“出差”字样而无法授权的问题。
5. 知识库片段、搜索结果、对话历史及任务结果统一标记为不可信数据，明确禁止其中的提示词、角色切换和工具调用指令影响系统。
6. 政策、天气、记忆、偏好、行程和合规等结构化 Skill 结果不再经过第二个 LLM 改写，而是直接进入确定性 AnswerDocument 渲染，避免结论被二次改写或产生非数字型幻觉。
7. 修复附件文件名进入 HTML 属性造成的 DOM XSS 风险，并增加 CSP、`nosniff`、Referrer Policy 和 Permissions Policy。
8. 前端管理入口根据真实用户角色显示；知识库轮询增加离开页面取消、失败上限和指数退避；正文、卡片、移动端摘要和辅助信息字号全面提升。
9. Docker 的知识源目录改为可写挂载，与管理员上传功能保持一致；仍固定单 Uvicorn worker，符合 Milvus Lite 和进程内编排状态的当前拓扑约束。

## 2. 优化前问题与处理结果

| 等级 | 问题 | 可能影响 | 本轮处理 |
|---|---|---|---|
| 严重 | 所有登录用户都能上传、刷新全局制度库 | 普通员工可污染全公司问答依据 | 写操作统一使用管理员依赖，前端默认隐藏管理区，后端仍是最终权限边界 |
| 严重 | 全量刷新先删除旧集合 | 空目录、解析或 embedding 失败会使知识库整体不可用 | 新增蓝绿集合原子替换；任何预发布错误均保留旧集合 |
| 高 | 同名制度文件可被静默覆盖 | 正式制度可能被误替换且难以追溯 | 同名上传返回 409，要求使用新文件名 |
| 高 | Compose 将文档目录只读挂载 | 页面显示可上传，但运行时必然写入失败 | 基础与开发 Compose 均改为管理员 API 可写挂载 |
| 高 | “采购审批流程”等被关键词直接识别为差旅制度 | 回答错误领域政策，造成业务误导 | 区分差旅专有、通用和明确非差旅政策词；当前问题明确冲突时不继承旧行程语境 |
| 高 | “我喜欢靠窗”“我以前去过北京吗”需要额外差旅词才授权 | 偏好与个人记忆能力表现为随机失效 | 高置信度、用户隔离的 preference/memory 可直接授权 |
| 高 | RAG/搜索文本直接拼入模型提示 | 恶意文档或网页可进行提示注入 | 所有相关 system/user prompt 都增加不可信数据边界与只提取事实约束 |
| 高 | 结构化结果再次交给 Composer LLM 改写 | 政策或合规结论可能被改写；旧校验只能拦数字幻觉 | 六类结构化事实全部使用确定性渲染，保留 LLM 仅处理 general 输出 |
| 高 | 附件名插入 `title` HTML 属性 | 特制文件名可能突破属性并注入 DOM | 使用 `createElement`、`textContent`、属性赋值和事件监听器构建附件 chip |
| 中 | PDF 读取、文件哈希与文件写入在 async 路由中同步执行 | 大文档时阻塞事件循环 | 文件列表、全文读取、状态计算和落盘移入线程池 |
| 中 | 列表中的每个文档都重复读取 manifest | 文档数增长后产生 N 次磁盘读取 | 一次读取 manifest 后批量计算状态 |
| 中 | 900ms 刷新轮询无限重试 | 后端异常时持续制造请求并影响电量/网络 | 离开页面立即取消；最多连续失败 6 次；指数退避至 15 秒 |
| 中 | 非管理员也显示上传/刷新入口 | 误导用户，产生无意义失败操作 | 管理区默认 `hidden`，读取用户摘要里的 role 后再显示 |
| 中 | 多处正文和元信息仅 7–10px | 桌面显得“精致但难读”，移动端更明显 | 主正文 12–14px，必要的辅助信息 10–11px，按钮点击区同步增大 |
| 中 | 缺少基础浏览器安全响应头 | XSS 后果扩大、页面可被嵌入 | 增加 CSP、`X-Content-Type-Options`、Referrer/Permissions Policy |

## 3. 后端详细优化

### 3.1 知识库权限模型

读写权限现在明确分离：

```text
登录员工 ── GET 文档列表/全文 ──> 允许
管理员   ── POST 上传/刷新 ─────> 允许
普通员工 ── POST 上传/刷新 ─────> 403 FORBIDDEN
```

涉及文件：

- `webui_new/routes/knowledge_base.py`
- `webui_new/auth/deps.py`
- `webui_new/static/app.js`
- `webui_new/templates/chat.html`

前端角色隐藏只是体验优化，真正授权始终由 FastAPI 的 `require_admin` 完成，因此直接调用 API 也无法绕过。

### 3.2 RAG 全量刷新安全模型

旧流程：

```text
删除 live → 解析文档 → 生成向量 → 写 live
```

新流程：

```text
读取与解析全部文档
  ├─ 任一错误/零片段 ──> 返回 error，live 不变
  └─ 全部成功
       → 在触碰 live 前生成全部向量
       → 写 staging 集合
       → 校验 staging 行数
       → live 重命名为 backup
       → staging 晋升为 live
          ├─ 晋升失败 → backup 立即恢复为 live
          └─ 晋升成功 → 清理 backup（清理失败只记录告警）
```

涉及文件：

- `rag/vector_store.py`：增加明确的 `replace_chunks` 能力；默认不允许用“清空后写入”伪装原子替换。
- `rag/milvus_store.py`：实现蓝绿集合切换和回滚。
- `rag/pipeline.py`：全量刷新只有在全部文档成功解析、切片后才发布。
- `webui_new/knowledge_base_service.py`：只有成功报告才更新 manifest；失败保留旧 manifest。

真实 Milvus Lite 验证结果：旧集合含 `old policy`，替换后只检索到 `new policy`，集合列表仅剩正式集合，无 staging/backup 残留。

### 3.3 文件写入与性能

- 文档使用随机临时文件写入，调用 `fsync` 后再 `os.replace`，避免进程中断产生半文件。
- 同名文件返回 `409 KNOWLEDGE_DOCUMENT_EXISTS`，不再覆盖。
- 上传、列表解析、PDF 读取、SHA-256 和全文读取通过 Starlette 线程池执行，避免阻塞 async 事件循环。
- 文档索引状态由一次 manifest 读取批量计算，消除 N 次重复读取。

## 4. 意图识别优化

### 4.1 政策问题的领域消歧

规则拆成三组：

- 差旅专有：差旅政策、差旅制度、住宿标准、交通标准、差旅费、住宿费、餐补、饭补。
- 通用歧义：报销、发票、补贴、流程、标准、审批等，必须再有差旅证据。
- 明确非差旅：采购、医疗、医保、医药、学费、社保、年假、请假、办公用品、政府补贴。

明确非差旅词以本轮问题为准，即使上一轮正在讨论南京出差，也不会把“另外医疗费报销流程是什么”授权给差旅 RAG。模糊追问如“那报销流程呢”仍可以继承当前出差上下文。

### 4.2 偏好与个人记忆

preference 和 memory-query 都只操作当前认证用户的数据。高置信度识别后不再强制要求问题重复出现“出差/差旅”，因此下面输入可稳定工作：

- “我喜欢靠窗座位” → `preference`
- “我以前去过北京吗” → `memory_query`

信息查询仍保持更严格边界：天气、航班、铁路和公开交通信息必须与当前公司出差直接相关。

### 4.3 回归样例

新增或强化的关键样例：

- “餐补标准是多少” → `rag_knowledge`
- “采购审批流程是什么” → 不进入差旅 RAG
- “医疗费报销流程是什么” → 不进入差旅 RAG
- “政府补贴标准是多少” → 不进入差旅 RAG
- “我喜欢靠窗座位” → `preference`
- “我以前去过北京吗” → `memory_query`
- 历史为南京出差 + “另外医疗费报销流程是什么” → 不继承差旅政策上下文

## 5. 回复框架与可信边界

系统总体回复框架未改变：

```text
用户输入
  → Guard / Intent Router / IntentionAgent
  → Skill Catalog 声明驱动的任务分解与执行
  → TaskResult
  → AnswerDocument
  → 前端类型化卡片
```

本轮只调整 TaskResult 到 AnswerDocument 的最后一层：

- policy、weather、memory、preference、trip、notice 使用确定性 `FallbackComposer` 类型渲染。
- general 内容仍允许使用 AnswerComposer LLM，并继续通过 schema、Goal 覆盖和数值事实校验。
- sources 与 plain_text 仍由系统生成，模型无法伪造来源。
- 行程、政策、天气和合规结果不再经过第二次语言模型改写，减少幻觉并保证同一输入得到稳定卡片结构。

提示词可信边界覆盖：

- 意图模型：历史对话与长期记忆是不可信上下文。
- RAG 模型：用户问题与知识片段是不可信数据，只能提取制度事实。
- 搜索总结模型：网页标题和摘要是不可信外部数据，不能证明实时价格、余票或已预订。
- General Composer：任务结果引用文本是不可信数据，不得执行其指令。

## 6. 前端体验与安全优化

### 6.1 人性化与高端感

保留现有低饱和纸张色、细边框、克制阴影和品牌路线视觉，不更换设计语言。优化集中在可读性与交互确定性：

- 知识卡片标题提升到 14px，摘要 11px，移动端页面说明 12px。
- AnswerCard 主体 13px，事实值 13px，说明和来源 11–12px。
- 设置、状态、侧栏、Toast 和首次设置的辅助文字普遍提升 1–2px。
- 展开按钮增加最小高度，文件上传由 label 改为标准 button + 隐藏 input，键盘和读屏行为更明确。
- Toast 使用 `aria-live="polite"`。
- 桌面保持双栏“文档目录 + 阅读器”；390px 移动端自动改为单栏列表，选中文档后切换到阅读器。

### 6.2 XSS 与浏览器安全

附件文件名不再拼接到 `innerHTML`。所有文字使用 text node，`title`、dataset 和 `aria-label` 使用 DOM 属性 API，删除行为使用闭包绑定。

全站响应增加：

- `Content-Security-Policy`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`

CSP 限制脚本与网络连接为同源、禁止 object、禁止 base URI 劫持和第三方 frame 嵌入；保留同源图片/data 图片和现有内联样式兼容性。

### 6.3 轮询与状态

- 只有管理员且正在知识库页面时才轮询刷新状态。
- 离开页面立即取消 timer。
- 成功请求重置失败计数。
- 连续失败最多 6 次并指数退避，最终停止并给用户明确提示。

## 7. 验证记录

### 7.1 自动化测试

```text
核心定向回归：112 passed
容器全量回归：405 passed, 23 skipped
JavaScript 语法：node --check 通过
补丁格式：git diff --check 通过
Docker Compose 合并配置：config --quiet 通过
```

23 个 skipped 为项目原有的条件型/外部依赖测试，并非本轮新增失败。

新增测试覆盖：

- 普通员工可读但无法管理知识库。
- 同名文件拒绝覆盖。
- 刷新失败保留旧 manifest。
- 空目录、解析失败、向量切换失败保留旧向量。
- staging 成功晋升及晋升失败回滚。
- 非差旅政策反例和历史上下文污染反例。
- 偏好/个人记忆自然语言正例。
- 附件 DOM 安全构建、角色控制和静态资源 cachebuster。
- CSP 与 `nosniff` 响应头。
- 结构化结果绕过第二个 Composer LLM。

### 7.2 真实组件与运行态

- 在容器内使用真实 pymilvus/Milvus Lite 完成一次旧集合 → staging → 新集合切换，结果正确。
- 使用最新 Compose 配置强制重建 `hommey` 服务，容器恢复 healthy。
- 重新 build `hommey:latest` 镜像并确认内置 `pytest-asyncio==0.24.0`；避免旧镜像缺少插件时把 async 测试误报为 skipped。
- `/readyz` 返回 `ok: true`，以下 7 项均通过：API key、RAG embedding、Milvus 数据目录、单 worker 拓扑、MCP 配置、Redis、PostgreSQL。
- HTTP 实测包含 CSP、`nosniff` 和 request ID。

### 7.3 浏览器视觉验收

使用真实 Chrome Headless 和临时普通员工账号验证：

- 1440×1000：知识库双栏正常，10 份文档可见，管理区隐藏。
- 390×844：单栏正常，无水平溢出，标题 14px、摘要 11px、页面说明 12px。
- 临时账号、4 条偏好和 1 个空会话已在验收后精确删除。

## 8. 回滚断点

本轮保留了三档独立断点：

| 断点 | Commit | Branch | 用途 |
|---|---|---|---|
| 优化前 | `fb512b1` | `backup/pre-full-optimization-20260809` | 恢复到本轮开始前，同时保留用户原有未提交修改 |
| 核心优化后 | `1e608d5` | `backup/post-core-optimization-20260809` | 保留安全、RAG、意图、回复与第一轮 UX 修复 |
| 可读性优化后 | `2a3398f` | `backup/post-ux-optimization-20260809` | 保留核心优化和完整桌面/移动端字号优化 |
| 最终交付 | 本文档提交对应的 HEAD | `backup/final-full-optimization-20260809` | 包含完整代码、测试、部署与本报告 |

推荐的非破坏性回滚方式：

```bash
# 查看当前交付与优化前差异
git diff backup/pre-full-optimization-20260809..HEAD

# 从优化前断点创建独立恢复分支，不覆盖当前分支
git switch -c recovery/pre-full-optimization backup/pre-full-optimization-20260809

# 或在当前交付分支逐个创建反向提交（先回退较新的提交）
git revert 2a3398f
git revert 1e608d5
```

文档提交在 `2a3398f` 之后；若要让当前分支完全回到优化前状态，应先 revert 最终文档提交，或直接采用新建 recovery 分支的方式。

## 9. 运维说明与当前约束

1. 当前仍固定 `UVICORN_WORKERS=1`。原因是 Milvus Lite 单文件数据库和部分 Web 管理状态属于进程内状态；盲目增加 worker 会重新引入并发写入和状态不一致风险。
2. 管理员身份来自用户表的 role。新用户会根据 `HOMMEY_ADMIN_EMAILS` 在注册时确定角色；既有普通用户不会因修改环境变量自动升级，需要显式角色迁移。
3. 刷新任务的进度状态仍在单进程内存中；服务重启会终止正在执行的刷新并回到 idle，但蓝绿发布保证旧正式集合不会因此被删除。若未来需要跨进程任务恢复，应将刷新任务迁移到持久化队列。
4. 当前蓝绿切换使用 Milvus Lite 的集合重命名，数据安全有回滚保证；它不是多节点 Milvus alias 的跨集群零停机方案。当前单 worker 本地部署符合这一边界。
5. CSS 的 `style-src` 暂保留 `'unsafe-inline'`，用于兼容现有模板中的少量内联样式。脚本已严格限制为同源；后续若清理全部内联 style，可进一步移除该兼容项。

上述均为已知架构约束，没有发现会阻止当前单实例系统正常运行的未解决严重 Bug。

## 10. 主要文件清单

后端与意图：

- `.agents/skills/ask-question/script/agent.py`
- `.agents/skills/query-info/script/agent.py`
- `agents/intention_agent.py`
- `core/guard_rules.py`
- `core/intent_guard.py`
- `core/intent_router.py`
- `core/orchestration/composer.py`
- `rag/milvus_store.py`
- `rag/pipeline.py`
- `rag/vector_store.py`
- `webui_new/auth/deps.py`
- `webui_new/core/errors.py`
- `webui_new/knowledge_base_service.py`
- `webui_new/routes/knowledge_base.py`

前端与部署：

- `webui_new/static/app.js`
- `webui_new/static/hommey.css`
- `webui_new/static/answer-card.css`
- `webui_new/templates/chat.html`
- `webui_new/templates/login.html`
- `docker/docker-compose.yml`
- `docker/docker-compose.dev.yml`

回归测试：

- `tests/test_intent_guard.py`
- `tests/test_knowledge_base_routes.py`
- `tests/test_rag_agent.py`
- `tests/test_rag_pipeline.py`
- `tests/test_rag_production_pipeline.py`
- `tests/test_task_orchestration_v2.py`
- `tests/test_trip_intake_experience.py`
- `tests/test_answer_card_presentation.py`
