# LangGraph 多 Agent 架构迁移计划

## 目标与边界

将当前“意图识别 + 自定义调度器 + Skill Agent”的请求内编排，逐步迁移为可持久化的 LangGraph 工作流。迁移期间保持 Web、CLI、MCP 三个入口可用；PostgreSQL 继续作为业务事实源，Redis 继续只承担会话热缓存。

本计划不把 LangGraph 当作幂等、认证或业务数据存储的替代品：`request_id` 幂等、数据库约束、外部 API 幂等键仍由应用层负责。

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

`session_id` 映射为 LangGraph `thread_id`；`request_id` 是一次用户发送意图的幂等键，不能替代 `thread_id`。

## 迁移阶段

### 阶段 0：基线与契约冻结

1. 为当前 Web、CLI、MCP 的典型路径补齐回归用例。
2. 定义 Pydantic 的 `TravelState`、`IntentResult`、`SkillTask`、`SkillResult` 和统一错误模型。
3. 为现有 Skill 提供 `run(state) -> SkillResult` 适配层；首阶段仍可在适配层调用现有 AgentScope Agent。
4. 明确副作用分类：纯读取、可重试写入、必须审批的外部动作。

验收：现有功能行为不变，且状态/Skill 输入输出有自动化测试。

### 阶段 1：引入最小 LangGraph 顶层图

新增图节点：`load_context`、`intent`、`route`、`aggregate`、`persist_response`。

- `intent` 暂时复用 `IntentionAgent`。
- `route` 将意图结果转为 `SkillTask` 列表。
- `aggregate` 复用现有展示/结果聚合规则。
- Web、CLI、MCP 通过一个运行时 Facade 调图，避免三套分叉逻辑。

验收：单 Skill 路径完全经由图执行，并保持现有响应结构。

### 阶段 2：Skill 节点化与动态并行

将 `query-info`、`ask-question`、`preference`、`memory-query`、`event-collection`、`plan-trip`、`mcp-tool` 拆成节点或子图。

- 使用条件边选择 Skill；使用 `Send` 对同优先级 Skill 动态并行 fan-out。
- `event-collection -> plan-trip` 用显式依赖边，只有行程槽位完整时才进入规划。
- Skill 输出通过 reducer 合并，禁止节点直接改写其他节点的状态。

验收：多意图请求可并行执行；依赖步骤按正确顺序执行；失败 Skill 不阻断可降级回答。

### 阶段 3：持久化、暂停和恢复

1. 配置 PostgreSQL checkpointer，并以 `session_id` 作为 `thread_id`。
2. 缺少出发地、目的地或日期时，使用 `interrupt` 返回结构化追问；用户补充后从同一 checkpoint 恢复。
3. 为高风险 MCP/未来订票、报销、通知动作增加审批节点。
4. 将 checkpoint 生命周期与现有会话关闭、保留策略对齐。

验收：进程重启后可恢复被暂停的行程收集；审批前不会执行外部副作用。

### 阶段 4：可靠性与幂等闭环

1. 客户端为一次“点击发送”生成并在重试中复用 `X-Request-ID`。
2. 在图入口以 `(user_id, request_id)` 原子领取执行权，区分 `processing`、`completed`、`failed`。
3. 已完成请求返回保存结果；处理中请求等待、订阅状态或返回可轮询响应。
4. 节点调用外部系统时传递独立幂等键；写入结果后再推进执行状态。

验收：丢响应后的重试不重复调用模型或写入业务结果；并发同键请求不产生双执行。

### 阶段 5：去除 AgentScope 依赖

完成全部 Skill 节点迁移后，依次替换：

1. `AgentBase` / `Msg` 为项目 DTO 与节点函数；
2. `OpenAIChatModel` 为独立模型客户端或 LangChain 模型适配；
3. AgentScope MCP Client 为官方 MCP SDK 或经验证的 LangGraph/LangChain 适配；
4. 删除 `config_agentscope.py`、AgentScope 初始化和依赖。

验收：运行时、CLI、Web、MCP 和全部测试均不再导入 `agentscope`。

## 发布策略与回滚

- 每个阶段单独分支、单独 PR，禁止大爆炸式替换。
- 通过 feature flag 选择 legacy 或 LangGraph Facade，先在测试/预发布启用。
- 保留现有 PostgreSQL 数据模型；LangGraph checkpoint 使用独立表或独立 schema。
- 任一阶段出现正确性、延迟或成本回归时，切回 legacy 编排，不回滚业务事实数据。

## 建议的实施顺序

优先完成阶段 0、1 和 2，先获得可观察的图式并行编排；阶段 3、4 再处理长流程与可靠性；只有功能等价后才执行阶段 5 的 AgentScope 移除。
