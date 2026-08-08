# Skill 平台 v1 升级说明

> 架构说明：本文记录 Skill 平台 v1 最初采用的 Manifest 方案。当前 Skill 已迁移为标准 `SKILL.md` frontmatter + 可选 `hommey.yaml` 扩展，参见 [2026-07-17 标准 Agent Skill 架构迁移](2026-07-17-standard-agent-skill-architecture.md)。

## 1. 目标

本次升级把 Hommey 从“意图、Prompt 和 Agent 分散配置”改造成以 Skill Manifest 为核心的企业差旅系统，目标是：

- 降低 Prompt 重复和冲突。
- 让差旅问答、出差事项和合规检查成为可复用业务 Skill。
- 用声明式依赖替代 Python 中的硬编码调度表。
- 为后续完整 Skill 平台保留版本、权限、Schema、管理和可观测能力。
- 保持 FastAPI Web、AgentScope、RAG、记忆和现有 API 基本兼容。

## 2. 关键设计决策

### 2.1 Skill 与公司知识分离

Skill 保存稳定流程，例如如何检索、判断、拒答和展示来源；公司具体补贴、住宿上限和报销规定继续存放在 RAG 文档中。禁止把公司制度数值写死到 Skill Prompt。

### 2.2 SKILL.md 与 Manifest 分离

`SKILL.md` 只保存具体执行方法和流程，不保存 name、description 等元数据。机器运行信息全部放在 `manifest.yaml`：

- `name` / `version` / `display_name`
- `category` / `domain` / `intent`
- `agent_name` / `entrypoint`
- `tools` / `risk_level`
- `requires` / `execution`
- `input_schema` / `output_schema`
- `enabled_by_default`

`core/skill_manifest.py` 使用 Pydantic 验证命名、版本、已知工具、入口文件和 Schema 路径。目录缺少 Manifest 或资源时，应用会尽早失败，而不是在用户请求中静默出错。

### 2.3 工具权限

Manifest 只接受平台允许的工具标识：

```text
active_trip_context
rag_retrieval
travel_information
weather
web_search
memory
mcp
```

`mcp-tool` 默认停用且不是面向用户的业务 Skill。支付、预订、审批和报销提交仍由领域门禁禁止。

## 3. 首批业务 Skill

### 企业差旅问答 `ask-question`

- 只依据企业 RAG 文档回答。
- 无证据时返回知识库无相关信息。
- 输出来源文件；元数据允许时附页码和章节。
- 不用模型常识补充制度数值或审批规则。

### 出差事项收集 `event-collection`

- 每位用户维护一个当前出差任务。
- 新信息增量合并，不能用 null 覆盖已有字段。
- 记录目的地、日期、工作地点、工作时间和缺失信息。
- 当前版本不支持同时切换多个未完成任务。

### 合规行程检查 `check-trip-compliance`

- 读取结构化出差事项、拟定行程和 RAG 证据。
- 逐项输出 `compliant`、`non_compliant` 或 `unknown`。
- 每个确定结论必须有制度来源。
- 没有 RAG 证据时固定返回 `verdict=unknown`。
- 仅提供预检查建议，不替代财务或主管审批。

### 合规差旅行程规划 `plan-trip`

声明式执行流程：

```text
Priority 1: event-collection + ask-question
Priority 2: plan-trip
Priority 3: check-trip-compliance
```

调度关系来自 `plan-trip/manifest.yaml`，不再写死在 `schedule_builder.py`。

## 4. 当前出差任务

新增 `active_trip_contexts`。PostgreSQL 使用 JSONB 保存可扩展任务内容；本地 file memory 使用 `active_trip` 字段提供兼容实现。

聊天页右侧展示当前任务。Web 意图识别会把当前任务作为 system context 注入，因此跨会话的“补贴呢”“怎么走”“检查是否合规”仍可关联到同一任务。

## 5. 数据库迁移

应用在 PostgreSQL 模式启动时按文件名顺序执行 `webui_new/auth/migrations/*.sql`，并在 `schema_migrations` 保存版本和 SHA-256 校验值。

`0002_skill_platform.sql` 非破坏性地增加：

- `users.role`
- `active_trip_contexts`
- `skill_settings`
- `skill_execution_runs`
- 执行轨迹查询索引

已执行迁移的内容不得原地修改；应新增更高版本迁移。迁移不删除现有用户、聊天、偏好或历史行程。

## 6. 管理员与环境配置

`.env`：

```bash
HOMMEY_ADMIN_EMAILS=admin@example.com,owner@example.com
```

配置邮箱新注册时写入 `role=admin`；已有用户会在启动迁移后提升为管理员。普通用户访问 Skill 管理 API 返回 403。

管理员页面：

```text
/admin/skills
```

API：

```text
GET   /api/admin/skills
GET   /api/admin/skills/{skill_name}
PATCH /api/admin/skills/{skill_name}/enabled
```

页面提供 Skill 列表、版本、分类、风险、工具权限、启停、静态依赖图和最近执行轨迹。v1 不允许在线编辑 Prompt 或发布新版本，正式内容仍通过 Git 管理。

## 7. 执行轨迹与隐私

`skill_execution_runs` 保存：

- Skill 名称和版本
- 状态和耗时
- 意图与结构化实体摘要
- 证据数量
- 错误码
- 同一次请求的 request id

默认不保存完整用户问题、完整回答、完整 Prompt、RAG 原文或凭据。

## 8. 来源展示

RAG Agent 统一返回：

```json
{"file":"差旅管理制度.pdf","page":12,"section":"住宿标准"}
```

前端按实际元数据渐进展示：文件名必选，页码和章节可选。知识库没有可靠来源时，合规 Skill 不输出确定结论。

## 9. 兼容与迁移

- Web 聊天和现有 API 路径保持。
- Agent 名保持兼容，由 Manifest 的 `agent_name` 映射到 Skill。
- CLI 不新增管理功能，只保持现有调用尽量不破坏。
- 原 `intent_catalog.py` 和 `schedule_builder.py` 仍提供旧接口，但数据改由 Manifest 生成。
- 新增 Skill 必须同时提供 `SKILL.md` 与 `manifest.yaml`；带 Agent 的 Skill 还需提供 Manifest 声明的入口文件。

## 10. 新增 Skill 流程

1. 使用 Skill 初始化脚本创建目录。
2. 编写简洁 `SKILL.md`。
3. 添加 `manifest.yaml`、必要 Schema、References 和执行器。
4. 为触发、越界、无证据和正常路径增加测试。
5. 运行 Skill 快速校验与项目测试。
6. 通过 Git 评审后部署；管理员页面只负责启停和观察。

## 11. 当前限制

- 每位用户只有一个当前出差任务。
- 未接入官方票务 API，公开交通信息不能证明实时余票和价格。
- 管理页面不在线编辑 Skill。
- 合规检查质量依赖企业文档的完整性和 RAG 检索质量。
