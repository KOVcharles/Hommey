# LangGraph 多 Agent 架构迁移计划

> 状态：已被 [任务级编排架构](../task-orchestration-v2.md) 取代，仅保留作历史设计记录。当前运行时不使用 LangGraph checkpointer，也不应按本文继续实现第二套状态源。

## 目标与边界

将当前“意图识别 + 自定义调度器 + Skill Agent”的请求内编排，逐步迁移为可持久化的 LangGraph 工作流。迁移期间保持 Web、CLI、MCP 三个入口可用；PostgreSQL 继续作为业务事实源，Redis 继续只承担会话热缓存。

本计划不把 LangGraph 当作幂等、认证或业务数据存储的替代品：`request_id` 幂等、数据库约束、外部 API 幂等键仍由应用层负责。

## 迁移动机与收益

本迁移的首要目标是**系统化学习 LangGraph**：此前 AgentScope 基本未投入使用，实际编排是手搓 asyncio，缺乏标准化的状态机与执行模型。借 LangGraph 重建工作流，既能掌握状态图 / 条件边 / `Send` 并行 / `interrupt` / checkpointer 模型，也顺带获得相对手搓编排的架构收益：

1. **可持久化执行态**：长流程（行程信息收集）可在进程重启后恢复，不再依赖进程内 `results` 列表。
2. **正规化的 interrupt/resume**：缺槽位追问、高风险动作审批由图的 `interrupt` 统一承载，替代手写 `_pause_incomplete_trip_planning`。
3. **标准化可观测图**：节点/边的执行轨迹天然结构化，便于排障与回归。

> 学习提示：建议在阶段 0 之前先用一个脱离业务的极简 `StateGraph` 示例熟悉核心 API（`StateGraph` / 条件边 / `Send` / `interrupt` / `PostgresSaver`），再套到 Hommey 上下文，避免业务复杂度掩盖框架概念。

迁移基线（作为“迁移前后功能等价”的回归对照，而非商业 ROI 论证）：单请求平均 agent 调用数、端到端延迟 P50/P95、失败率，需在阶段 0 测量并记录。

## 目标架构

```text
入口（Web / CLI / MCP）
  -> request_id 幂等门
  -> LangGraph: intent -> route -> skill fan-out -> aggregate -> persist
                              |                    |
                              |                    -> 失败重试 / 降级
                              -> 缺槽位或高风险动作：interrupt / resume

PostgreSQL：会话、消息、业务记忆、幂等记录、LangGraph checkpoint
Redis：最近对话热缓存
```

统一图状态 `TravelState` 至少包含：`user_id`、`session_id`、`request_id`、消息、结构化意图、行程槽位、待执行任务、各 Skill 结果、待追问/待审批项、错误与最终回复。

`thread_id` 采用 `{user_id}:{session_id}` 格式，而非直接用裸 `session_id`——当前 Web 的 `session_id` 仅取 UUID 前 8 位（`webui_new/manager.py` 第 61 行），32-bit 且无租户隔离，不适合作为分布式 checkpoint 的线程标识。读取线程须在鉴权后按 `user_id` 校验归属。`request_id` 是一次用户发送意图的幂等键，不能替代 `thread_id`。

## 迁移阶段

### 阶段 0：基线与契约冻结

1. 为当前 Web、CLI、MCP 的典型路径补齐回归用例。
2. 定义 Pydantic 的 `TravelState`、`IntentResult`、`SkillTask`、`SkillResult` 和统一错误模型。
3. 为现有 Skill 提供 `run(state) -> SkillResult` 适配层；首阶段仍可在适配层调用现有 AgentScope Agent。
4. 明确副作用分类：纯读取、可重试写入、必须审批的外部动作。

**前置约束（务必先于阶段 1 落地）：**

- **状态分层契约（防“双真相源”）**：明确 checkpoint 与现有记忆系统（PostgreSQL `chat_history`、Redis 短期记忆、`active_trip_contexts`）的分工。checkpoint 只存“图执行所需的中间态、行程槽位、待追问/待审批项”；消息落库仍由记忆层 `MemoryManager` 负责；`active_trip_contexts` 退化为 checkpoint 的业务索引或由其派生。
- **依赖引入与版本矩阵验证**：在隔离 venv 锁定 `langgraph` 与 `langgraph-checkpoint-postgres`（或 async saver）版本，验证与 `pydantic>=2.11,<3`（被 `agentscope==1.0.16` 的 `mcp>=1.13` 强制）、`psycopg[binary]`、Python 版本四元组的共存；跑通最小图 + PostgresSaver 建表。当前 `requirements.txt` 零 LangChain 生态包。
- **`TravelState` schema 作为独立交付物**：现有无共享状态、数据靠 `Msg.content` 的 JSON 流动，`TravelState` 一旦定下后续每个节点都要适配，需单独评审其字段与 reducer，而非在阶段 1 轻量带过。
- **契约测试黑盒化**：阶段 0 的回归用例应测“图入口→出口的输入输出契约”（黑盒），而非内部节点。现有 `tests/test_orchestration.py` 直接 stub `AgentBase.reply`，迁移到节点后须重写——若阶段 0 测内部节点，阶段 1 落地时测试大改，达不到“契约冻结”目的。
- **manifest → SkillTask 转换层**：现有 `hommey.yaml` 已声明 `execution`、`requires`、`priority`、`on_failure`、`max_retries`、启停与版本等语义（见 `core/skill_definition.py` 的 `HommeySkillConfig`）。图的 Skill 选择与依赖应由这层声明式 manifest 转换而来，**禁止在图里硬编码 Skill 名称与依赖**；转换层只需一个把 manifest 翻译成条件边 / `Send` 的函数，不必过度设计。
- **消息持久化责任归属**：明确最终由 Facade 或图节点 `persist_response` **二选一**负责消息落库，避免迁移期双写。现状是 Web 入口在编排前后分别写 user / assistant 消息（`webui_new/manager.py` 第 473、510/513/518 行），引入图后须避免重复写入，且 `interrupt` 暂停时不能只落半个回合（user 已入库而 assistant 未生成）。
- **interrupt 节点副作用规范（全局）**：LangGraph 从 `interrupt` 恢复时会**从头重放被中断节点**，因此该节点内 `interrupt()` 之前的所有副作用（写库、MCP 调用、通知）一律**后置于 `interrupt()` 之后**，或携带独立幂等键 / outbox 保障。此规范适用于任何含 `interrupt` 的节点，从阶段 0 即确立。

验收：现有功能行为不变，且状态/Skill 输入输出有自动化测试；前置约束七项均有书面结论。

### 阶段 1：引入最小 LangGraph 顶层图

新增图节点：`load_context`、`intent`、`route`、`aggregate`、`persist_response`。

- `intent` 暂时复用 `IntentionAgent`。
- `route` 将意图结果转为 `SkillTask` 列表。
- `aggregate` 复用现有展示/结果聚合规则。
- Web、CLI、MCP 通过一个运行时 Facade 调图，避免三套分叉逻辑。

验收：单 Skill 路径完全经由图执行，并保持现有响应结构。

### 阶段 2：Skill 节点化与动态并行

将 `query-info`、`ask-question`、`preference`、`memory-query`、`event-collection`、`plan-trip`、`check-trip-compliance` 拆成节点或子图（`mcp-tool` 默认禁用，按需纳入）。迁移范围以 `hommey.yaml` 的声明式契约为准，避免在图里硬编码遗漏。

- 使用条件边选择 Skill；使用 `Send` 对同优先级 Skill 动态并行 fan-out。
- `event-collection -> plan-trip` 用显式依赖边，只有行程槽位完整时才进入规划。
- Skill 输出通过 reducer 合并，禁止节点直接改写其他节点的状态。

**本阶段约束：**

- **pause/resume 进程内等价**：本阶段尚无 checkpoint，`event-collection -> plan-trip` 的依赖判定仍走进程内同步逻辑（复用现 `_pause_incomplete_trip_planning` / `_continue_ready_trip_planning` 的等价语义，不持久化）。正式的跨进程 `interrupt` + checkpoint resume 在阶段 3 一次性切换，本阶段禁止新旧两套恢复机制并存。
- **共享纯函数抽取清单**：图的 `aggregate` 节点要复用现有聚合/记忆更新规则，须先把 `OrchestrationAgent._aggregate_results`、`_update_memory` 等从 asyncio 编排耦合中抽成可复用纯函数；本阶段列出抽取清单并在 feature flag 两侧共用。

验收：多意图请求可并行执行；依赖步骤按正确顺序执行；失败 Skill 不阻断可降级回答；此阶段 pause/resume 行为与现状等价，未引入跨进程恢复。

### 阶段 3：持久化、暂停和恢复

1. 配置 PostgreSQL checkpointer，`thread_id` 采用 `{user_id}:{session_id}`（见“目标架构”），鉴权后按 `user_id` 校验线程归属。
2. 缺少出发地、目的地或日期时，使用 `interrupt` 返回结构化追问；用户补充后从同一 checkpoint 恢复。
3. 为高风险 MCP/未来订票、报销、通知动作增加审批节点。
4. **checkpoint 生命周期与清理**：明确删除会话、清空历史、会话保留期分别如何清理 / 保留 checkpoint；`active_trip_contexts`（每用户一个当前行程）与每会话 checkpoint 之间确定唯一写入方与恢复规则（前者派生自后者，或作为业务索引）。

**前置验收（务必先于 interrupt 上线满足）：**

- **同会话串行化**：阶段 3 引入 checkpoint 后，同 `thread_id` 的双击、网络重试或多实例会并发恢复 / 推进同一会话。须在 interrupt 上线前引入最小串行化——单实例进程内锁或 DB 行锁（`SELECT FOR UPDATE`）兜住并发；完整的执行权领取表与重放留到阶段 4。
- **thread_id 安全**：`{user_id}:{session_id}` 格式落地，鉴权后才能读取线程（防止越权读取他人 checkpoint）。
- **interrupt 恢复不重复副作用**：验收“进程在写入前 / 写入后、checkpoint 前 / 后”被中断后恢复，均不重复产生外部副作用（依据阶段 0 的 interrupt 节点副作用规范）。

验收：进程重启后可恢复被暂停的行程收集；审批前不会执行外部副作用；前置验收三项均通过。

### 阶段 4：可靠性与幂等闭环

1. 客户端为一次“点击发送”生成并在重试中复用 `X-Request-ID`。
2. 将阶段 3 的最小串行化升级为完整的 `(user_id, request_id)` 原子执行权领取（`request_execution` 表），区分 `processing`、`completed`、`failed`，并补齐重放与轮询。
3. 已完成请求返回保存结果；处理中请求等待、订阅状态或返回可轮询响应。
4. 节点调用外部系统时传递独立幂等键；写入结果后再推进执行状态。

**双层幂等职责分工（与现有记忆层幂等对齐）：**

现有记忆层已有 `request_id` 幂等（`MemoryManager.add_message` 冲突不重写、`get_recorded_response` 重放；`long_term_memory` 的 `ON CONFLICT DO NOTHING` 与 `(user_id, request_id)` 唯一约束）。本阶段须明确两层分工，避免重叠：

- **图入口幂等 = 执行权领取**：防止并发同键双执行（`processing` / `completed` / `failed`）。
- **记忆层幂等 = 数据落地去重**：防止重复写业务结果。

执行顺序约定：领取执行权 → 执行图 → 写记忆（去重兜底）→ 推进执行状态为 `completed`；图层失败时需说明记忆层如何补偿（已写入部分结果的可见性与回滚策略）。

验收：丢响应后的重试不重复调用模型或写入业务结果；并发同键请求不产生双执行；两层幂等职责在文档与代码中一致。

### 阶段 5：去除 AgentScope 依赖

完成全部 Skill 节点迁移后，依次替换：

1. `AgentBase` / `Msg` 为项目 DTO 与节点函数；
2. `OpenAIChatModel` 为独立模型客户端或 LangChain 模型适配；
3. AgentScope MCP Client 为官方 MCP SDK 或经验证的 LangGraph/LangChain 适配。**注意范围**：本项目 `mcp-tool` skill 走的是自有的 `hommey_mcp/mcp_manager.py`，需先确认 AgentScope 内置 MCP client 是否实际被用到，仅替换真正依赖 AgentScope MCP 的路径，避免误伤自有 MCP 实现。
4. 删除 `config_agentscope.py`、AgentScope 初始化和依赖。

**意图识别单独评估**：`IntentionAgent` 的结构化输出（`routing` / `intents` / `key_entities`）只负责识别和授权，执行图由 Skill 声明经 `TaskGraphBuilder` 编译。其节点化与输出契约迁移不应与 `AgentBase/Msg` 替换混为一谈，需单独评估与回归，避免授权行为在移除阶段发生隐性改变。

验收：运行时、CLI、Web、MCP 和全部测试均不再导入 `agentscope`；意图识别输出契约迁移有独立回归用例。

## 发布策略与回滚

- 每个阶段单独分支、单独 PR，禁止大爆炸式替换。
- 通过 feature flag 选择 legacy 或 LangGraph Facade，先在测试/预发布启用。
- 保留现有 PostgreSQL 数据模型；LangGraph checkpoint 使用独立表或独立 schema。
- 任一阶段出现正确性、延迟或成本回归时，切回 legacy 编排，不回滚业务事实数据。

## 建议的实施顺序

优先完成阶段 0、1 和 2，先获得可观察的图式并行编排；阶段 3、4 再处理长流程与可靠性；只有功能等价后才执行阶段 5 的 AgentScope 移除。
