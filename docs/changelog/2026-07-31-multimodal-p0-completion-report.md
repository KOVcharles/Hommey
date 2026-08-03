# 多模态 P0 与阶段 A/B 完整修改报告

> 完成日期：2026-07-31
> 基线：`origin/main` / `5e1f498`
> 工作分支：`codex/new20260731`
> 状态：代码、全量测试、PostgreSQL/Redis 集成测试、Docker 构建与健康检查通过；尚未提交 Git commit。

## 1. 修改范围

本次完成两部分工作：

1. 修复多模态路线阶段 A/B 中的数据库、消息持久化、附件绑定和上下文传递问题；
2. 完成路线文档 §8 定义的 P0：上传、私有存储、附件状态、TXT/MD/DOCX 解析、聊天附件契约、消息关联和前端附件卡片。

没有引入 P1/P2 的 ASR、OCR、旧 DOC 转换、持久任务队列、图片理解或原生多模态模型网关。

## 2. A1 数据库问题的根因

迁移器使用文件名第一个下划线前的文本作为版本号：

```python
version = path.stem.split("_", 1)[0]
```

原目录同时存在 `0004_chat_session_titles.sql` 和 `0004_memory_stage1.sql`。两者都会被记录为版本 `0004`：第一个执行后，第二个会命中同一版本，但 checksum 不同，因此迁移停止。结果是应用代码可能已经开始使用 `conversation_sessions` / `conversation_messages`，数据库却没有对应事实表。

修复原则：不修改已经发布的 `0001–0005`，不改变历史 checksum，不删除旧 `chat_history`，只增加新版本。

- `0006_memory_stage1.sql`：建立 UUID 会话、消息和版本事实表；
- `0007_conversation_message_attachments.sql`：建立 UUID 消息与附件的正式关联表；
- 旧 `chat_message_attachments(BIGINT)` 只作为已发布结构保留，运行时不再写入。

## 3. 修复后的数据模型

```text
conversation_sessions
    session_id UUID PK
          │
          └── conversation_messages
                  message_id UUID PK
                  request_id UUID
                  UNIQUE(user_id, request_id, role)
                          │
                          └── conversation_message_attachments
                                  message_id UUID FK
                                  attachment_id TEXT FK UNIQUE
                                          │
                                          └── attachments
                                                  │
                                                  └── attachment_extractions
```

关键约束：

- 一条附件只能绑定一条用户消息；
- 所有权、`ready` 状态和过期时间在消息事务内重新检查；
- 消息写入、session 计数、memory version 和附件关联共同提交或共同回滚；
- 同一个 request ID 重试时不能更换附件集合；
- 删除会话会物理删除消息并通过 FK cascade 删除关联，但保留附件元数据和原对象，供后续延迟清理。

## 4. P0 请求链路

```text
浏览器选择文件
  → POST /api/{user_id}/attachments
  → 流式限额读取
  → 扩展名 + 魔数 + DOCX ZIP 安全校验
  → data/uploads/{user_id}/{attachment_id} 私有保存
  → attachments(status=processing)
  → TXT/MD/DOCX 同步解析
  → extraction + status=ready 原子提交

浏览器发送聊天
  → POST /api/{user_id}/chat/stream
  → {message, attachment_ids} + X-Request-ID
  → AttachmentService.normalize
      ├── display_message：用户文字 + 文件清单
      └── agent_query：用户文字 + 受预算限制的不可信附件正文
  → 意图识别使用 agent_query
  → Orchestration request_context 保留完整 agent_query
  → Skill 优先读取 context.agent_query
  → conversation_messages 只写 display_message
  → 流式返回 sources / warnings / response
```

## 5. P0 验收映射

| P0 项目 | 实现结果 |
| --- | --- |
| 上传接口 | `POST /api/{user_id}/attachments`，multipart 上传，认证用户与路径用户必须一致 |
| 私有存储 | 文件位于非静态目录 `data/uploads/{user_id}/{attachment_id}`；对象键经过根目录约束，禁止 `..`、绝对路径和覆盖 |
| 附件状态 | `processing -> ready/failed`；提取结果与 `ready` 状态同事务可见；过期状态对外可见且不能再次绑定 |
| TXT/MD | UTF-8、UTF-8 BOM、GB18030、GBK 解码兜底 |
| DOCX | 标题、段落、列表和表格转文本/Markdown；损坏、伪造、加密、路径越界、超条目、超解压体积和异常压缩比被拒绝 |
| 聊天契约 | `message` 可空，但必须至少存在文字或附件；老客户端只传 `message` 仍兼容 |
| 消息关联 | UUID message ID 原子绑定；所有权、ready、TTL、重复绑定和幂等附件集合均校验 |
| 记忆隔离 | 附件全文不进入消息事实表或短期记忆，只有 `display_message` 入库 |
| Agent 上下文 | 完整 `agent_query` 绕过可能有损的 intent rewrite，传入下游 Skill |
| 前端 | 待发送附件、用户消息附件卡片、历史附件卡片、响应来源卡片和 warnings 展示 |
| 请求幂等 | 上传和聊天共用 `X-Request-ID`；失败重试保留文字、附件和 ID，编辑后生成新 ID |

当前还保留文字型 PDF 的同步解析能力，但它不是本次 P0 完成判定的依赖；扫描 PDF、OCR 和可靠异步处理仍属于 P1。

## 6. 安全收口

### 上传边界

- 路由按块读取，超过大小上限立即停止；
- 类型由扩展名与魔数共同确认；
- 文件名不进入对象存储路径；
- 所有 SQL 参数化；
- 日志不记录附件正文；
- 数据库元数据创建失败时删除已写原文件。

### DOCX 边界

- 必须是有效 ZIP，并包含 `[Content_Types].xml` 和 `word/document.xml`；
- 拒绝加密条目和 ZIP 内部路径穿越；
- 限制 ZIP 条目数、总解压体积和压缩比；
- 解析异常只写错误码，不向客户端暴露内部路径或堆栈。

### Prompt injection 边界

附件正文被包裹在明确的“不可信附件内容”边界内；系统提示要求不得执行附件中的命令、权限请求和工具调用指令。上下文设置总字符预算，超出时产生可展示 warning。

## 7. 主要代码修改

### 数据库与记忆

- `context/memory_repository.py`：UUID 事实表 repository、附件原子绑定、session 激活和兼容会话接口；
- `context/memory_service.py`：统一 PostgreSQL 事实源与最近记忆 facade；
- `context/memory_manager.py`：返回真实 message ID，统一 session 和 request/turn ID；
- `webui_new/auth/migrations/0006_memory_stage1.sql`；
- `webui_new/auth/migrations/0007_conversation_message_attachments.sql`。

### 多模态模块

- `multimodal/validation.py`：大小、数量、魔数、DOCX ZIP 安全校验；
- `multimodal/storage.py`：私有本地对象存储与根目录逃逸防护；
- `multimodal/document_processor.py`：TXT/MD/DOCX 解析；
- `multimodal/repository.py`：共享 PostgreSQL pool 和附件状态事务；
- `multimodal/service.py`：上传状态机、TTL、规范化与错误清理；
- `multimodal/context_builder.py`：`agent_query` / `display_message` 分离、预算和不可信边界。

### Web 与编排

- `webui_new/routes/attachments.py`：上传、状态和删除 API；
- `webui_new/manager.py`：单一规范化入口、持久化边界和 sources/warnings；
- `agents/orchestration_agent.py`：显式 `request_context`；
- `.claude/skills/*/script/agent.py`：下游优先消费完整 `agent_query`；
- `webui_new/static/app.js`：上传、附件卡片、幂等失败重试；
- `runtime.py` / `docker/Dockerfile`：Docker Web 后端单入口。

### 依赖

将 MCP 限制为 `mcp>=1.13,<2.0`。原因是 `agentscope==1.0.16` 仍调用 MCP 1.x 的 `streamablehttp_client`；不加上限时新镜像会安装 MCP 2.0 并在应用导入阶段失败。

## 8. 测试与部署结果

### 静态验证

- Python 修改文件 `py_compile`：通过；
- `node --check webui_new/static/app.js`：通过；
- `git diff --check`：通过；
- 基础与测试 Docker Compose config：通过。

### 自动测试

- P0 专项与相关回归：`56 passed`；
- 不启动外部服务的全量测试：`221 passed, 11 skipped`；
- PostgreSQL/Redis 集成专项：`9 passed`；
- 带 PostgreSQL/Redis 的最终全量测试：`230 passed, 2 skipped`；跳过项为默认关闭的真实 LLM 集成测试。

覆盖内容包括：

- UTF-8/GBK 文本、DOCX 标题/段落/表格；
- DOCX 损坏、内部路径越界和解压上限；
- 私有对象键越界和覆盖；
- 上传状态机与 DB 失败文件清理；
- 空文本 + 附件、跨用户访问、上传限额；
- 附件过期、未 ready、跨用户附件；
- 消息/附件原子提交、失败回滚和幂等附件集合；
- 删除会话后的关联清理；
- 完整 `agent_query` 进入下游 Agent；
- 现有鉴权、会话、记忆、Skill 和 Web 错误契约回归。

### Docker 实际部署

- 镜像 `hommey:latest` 重建成功；
- 应用命令确认为：

```text
uvicorn webui_new.server:app --host 0.0.0.0 --port 8000
```

- `hommey-app`、`hommey-postgres` 健康；
- `GET http://127.0.0.1:8000/healthz` 返回 `{"ok": true}`；
- 现有数据库 migration 记录为唯一版本 `0001–0007`；
- `conversation_messages` 和 `conversation_message_attachments` 已实际创建。

## 9. 兼容性与架构影响

该修改没有改变 Agent 的业务职责，也没有把解析逻辑塞入 route/manager。新增边界集中在：

- route：HTTP 和鉴权；
- attachment service：上传、状态与规范化；
- memory repository：消息事实和原子绑定；
- orchestrator：显式传递规范化上下文；
- Skill：消费统一 query。

旧纯文本 API 保持兼容；旧数据库表保留；已发布 migration checksum 不变。主要架构变化是 PostgreSQL 正式成为 Docker 运行时的消息事实源，Redis 只承担最近窗口缓存。

## 10. 明确不在 P0 的内容

以下内容仍按路线进入 P1/P2，不应在 P0 代码中提前混入：

- 音频上传、录音 UI、ASR 和时间戳；
- 扫描 PDF OCR、旧 DOC 的 LibreOffice 隔离转换；
- 持久任务队列、worker 重试和任务恢复；
- ClamAV 等外部恶意文件扫描服务；
- 到期对象的后台物理清理任务和审计队列；
- 图片/票据理解、NativeMultimodalGateway；
- 用户确认后的知识库沉淀。

其中外部恶意文件扫描、非 root 隔离 worker 和可审计物理清理属于生产强化条件，应在开放不可信公网上传或进入 P1 长任务处理前完成。
