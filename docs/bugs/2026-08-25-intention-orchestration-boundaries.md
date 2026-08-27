# 意图与编排职责边界 Bug 记录

> 编号：BUG-2026-08-25-01  
> 发现日期：2026-08-25  
> Bug 状态：Phase 1 可复现问题已修复；结构性风险部分保留  
> 对应分支：`codex/intention-orchestration-v3`

## 1. 用户现象与风险

旧链路让 `IntentionAgent` 同时识别意图、决定 `should_call_skill`、产出全局实体和改写 Query，随后 `TaskDecomposer` 再调用一次 LLM 拆解任务。多意图场景因此存在以下问题：

1. 两个相同 intent 的独立目标会被 Validator 合并，例如“查北京天气和上海天气”只剩一个 Goal。
2. 全局 `key_entities.destination` 无法同时表达北京和上海，实体会串到错误节点。
3. 第二次 LLM 拆解可能遗漏、扩展或重新解释 Intention 已识别的用户目标。
4. 执行授权与语义识别耦合，旧 `should_call_skill=true` 可能被下游直接当作授权事实。
5. Validator 使用模型生成的 `rewritten_query` 作为 scope 校验基线，形成“模型用自己的输出证明自己的输出”的信任闭环。

## 2. 根因

- `intent` 被错误地同时用作能力类型和 Goal 主键。
- 语义事实、授权决定、执行计划和运行状态没有清晰协议边界。
- 组内实体被压扁成请求级单值字典。
- 两个 LLM 阶段都拥有 Query 改写权，但没有稳定的一对一目标身份。
- 旧兼容字段同时承担显示、路由和安全授权用途。

## 3. 已修复

| 项目 | 修复 | 验证 |
| --- | --- | --- |
| 同 intent Goal 被合并 | 删除 `_merge_by_intent`，按 `group_id/task_id` 保持独立 | 新增同意图双 Goal 测试 |
| 实体跨 Goal 污染 | 每个 `IntentGroup` 保存独立 entities；Executor 注入节点级 `key_entities` | 新增北京/上海实体隔离测试 |
| 二次 LLM 漂移 | `TaskDecomposer` 改为确定性一对一适配 | 测试 relation 到 dependency 的固定映射 |
| 意图层拥有授权 | 新增 `OrchestrationPolicy`，意图层不再输出执行授权 | guard 和低置信度回归测试 |
| 旧授权可被信任 | legacy adapter 丢弃旧标记并重新计算 | 新增伪造 `should_call_skill=true` 拒绝测试 |
| Scope 信任闭环 | Validator 优先使用可信 `original_query` | 编排 scope 测试 |
| Web 测试替身依赖旧授权 | 测试旧 envelope 补充置信度，内部 intent 改为公开 intent | `test_webui_error_responses.py` 21 passed |
| 被拒前置 Goal 留下悬空依赖 | `required_context` 前置未授权时策略同步拒绝目标；适配器只连接已授权 Goal | 新增依赖授权传播测试 |

## 4. 尚未修复

### 4.1 状态 v2 没有静态计划快照

恢复时仍需要根据当前 Skill manifest 重编译图，并通过 `graph_hash` 检查一致性。Skill 在暂停期间升级时，旧 Run 可能无法直接恢复。

状态：未修复，计划在 state v3 中引入 `plan.node_specs` 和版本化迁移。

### 4.2 回答交付存在崩溃窗口

编排生命周期更新 `answer_delivered` 与 manager 写入 assistant message 不是同一个事务边界。两者之间进程退出时，可能出现状态认为已交付但消息未落库，或相反。

状态：未修复，需在 Runtime 收口时设计 outbox/交付提交协议。

### 4.3 Runtime 控制职责仍分散

请求恢复和入口判断在 manager，DAG 在 pipeline，状态转换在 lifecycle，持久化在 state store，聚合由 composer 完成。职责已经可描述，但还没有统一 `OrchestrationRuntime` 门面。

状态：未修复；本阶段避免同时改状态机与执行生命周期。

### 4.4 复合请求使用请求级输入 Guard

当前 `guard_user_input` 先检查整个原始 Query。若一个复合请求同时包含允许和明确禁止的子句，策略可能拒绝所有组，而不是只拒绝违规组。

状态：已记录，未修复；需要先定义“部分授权时如何向用户展示被拒目标”的产品契约。

### 4.5 渐进式 Skill 选择尚未完成

本阶段只把意图授权移到策略层。Skill 元数据仍从 catalog/manifest 加载，GraphBuilder 再编译 execution；还没有独立的渐进式 Skill resolver 或按需 manifest 装载状态。

状态：未修复，后续需基于性能数据决定是否引入，不能只为改名增加组件。

### 4.6 本地 PostgreSQL DSN 配置错误

未覆盖环境变量时，当前本地 `.env` 的 PostgreSQL DSN 无法被 psycopg 解析，测试收集阶段等待连接池后失败。使用文件后端环境覆盖后测试可正常执行。

状态：环境问题，代码未修改。建议后续在启动时增加脱敏的 DSN 格式快速校验，避免连接池超时后才报告。Bug 文档不记录实际 DSN 内容。

### 4.7 完整回归中的既有或环境失败

首次完整测试结果为 `11 failed, 591 passed, 27 skipped, 7 errors`。其中 3 条与本次协议迁移有关的 memory/intention 测试已经修复并单独通过；剩余非通过项如下：

| 类别 | 数量 | 原因或现状 |
| --- | ---: | --- |
| Redis 集成 | 9 | 本机 `127.0.0.1:6379` 未启动；含 7 个 fixture error 和 2 个并发测试失败 |
| PostgreSQL memory 集成 | 1 | 测试强制 PostgreSQL backend，但本轮为避免读取错误本地 DSN 将 DSN 置空 |
| Windows 文本编码 | 1 | 测试以系统 GBK 读取 UTF-8 golden JSON，触发 `UnicodeDecodeError` |
| RAG baseline | 1 | 文件后端覆盖下 recall 为 0；与本次编排改动无调用关系 |
| 前端静态资源断言 | 2 | 资源版本计数与现有 HTML 不一致；本分支未修改前端资源 |
| Windows symlink | 1 | 知识库 symlink 边界测试受当前平台/权限条件影响 |

排除上述测试后，完整回归为 `594 passed, 27 skipped, 6 deselected`。这些问题未被本次分支修复，也不能用通过的子集掩盖。

## 5. 数据库影响

本次没有新增或修改迁移。`orchestration_runs.state JSONB` 仍是生产唯一可信来源，快照 schema 仍为 v2。新意图信封可以保存在现有 `intention_data` JSON 字段，因此 Phase 1 无需 DDL。

数据库适配只有在 state v3 开始时才需要进行，并必须包含：旧快照读取器、v2 到 v3 转换、回滚策略、暂停 Run 的恢复测试以及 `0022` 迁移。未完成这些工作前不得把 v3 标为完成。

## 6. 验收与剩余验证

已完成定向自动化回归，覆盖意图、策略、多意图隔离、DAG、状态恢复、并发、记忆、评估和 Web 错误响应。尚缺真实模型端到端、真实 PostgreSQL、Redis 多 worker 和部署后会话恢复验证，因此当前状态是“代码完成，待部署验证”，不是“生产完全关闭”。
