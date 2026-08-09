# 任务级编排架构

## 当前唯一主链路

所有已授权的 Skill 意图，无论单意图还是多意图，都进入同一条任务级 DAG 管线：

```text
IntentionAgent
  -> TaskDecomposer
  -> TaskValidator
  -> TaskGraphBuilder
  -> TaskExecutor
  -> child agents
  -> AnswerComposer
```

`webui_new/manager.py` 是请求入口。系统不再保留旧编排开关，也不存在按优先级列表执行的备用路径。

快速路由只是意图识别的优化：只有“可证明完整的单意图”才能短路模型。含“然后、同时、顺便、另外”等连接关系的请求必须走完整识别；模型若遗漏用户明确说出的天气、制度或规划子句，确定性候选会补齐语义意图，但不会生成执行步骤。

## 职责边界

- `IntentionAgent`：识别一个或多个意图、改写 query、给出置信度和 `should_call_skill`。它不选择 Agent、不生成步骤、不决定执行顺序。
- `TaskDecomposer`：把已授权意图变成彼此隔离的语义任务。模型不可指定 Agent 或工具；模型失败时使用确定性 fallback。
- `TaskValidator`：校验意图授权、query scope、副作用和依赖，拒绝越权或循环依赖。
- `TaskGraphBuilder`：读取 `.agents/skills/*/hommey.yaml` 的 `execution` 声明，绑定可信 Agent，展开工作流并构建 DAG。
- `TaskExecutor`：根据显式依赖边执行 DAG；priority 只决定同时 ready 节点的批次顺序，不能代替依赖。后继任务读取前序结果，并统一处理失败、暂停和重试语义。
- `OrchestrationAgent`：名称暂时为兼容保留，实际只是 DAG 使用的子 Agent 执行适配器和审计记录器，不拥有计划和流程状态。
- `AnswerComposer`：依据语义任务与执行结果生成统一答案；每个 section 带 `goal_id`，来源、数值事实及逐 Goal 覆盖仍受 validator 约束。

## 单意图与多意图

单意图和多意图使用同一数据模型。差别只在已授权的意图节点数量：

- 单步 Skill 意图通常编译为一个执行节点。
- 工作流 Skill 可以从一个意图展开多个执行节点。例如 `itinerary_planning` 展开为事项收集、制度查询、外部信息查询、行程规划和合规检查。
- 多意图先各自产生 scoped query，再在 DAG 层处理依赖与能力复用。同一次用户请求中的独立天气/制度 Goal 可以满足规划工作流的同类能力节点，但它仍保留自己的 Goal 身份、query、状态与答案分区。不同 Turn 追加的 Goal 不允许反向改变旧 Goal 的依赖图。

这个边界防止制度查询 Agent 收到混有天气问题的整段 query，也防止意图模型通过输出 Agent 名称绕过 Skill 授权。

## 暂停与恢复

全局执行状态由 `OrchestrationStateStore` 单点维护。PostgreSQL 的 `orchestration_runs.state` JSONB 是生产环境唯一真相源；文件后端仅用于开发和测试。子 Agent 不写全局状态，只返回 `TaskResult`，执行生命周期适配器在节点开始、节点提交、等待、中断和结束这些持久化边界更新状态。

状态层级为 `Thread(session) -> Run -> Turn -> Goal -> Node`：

- Thread 是用户可见的对话会话。
- Run 是该会话内一个可持续推进的任务容器；同一会话最多一个活动 Run。
- Turn 是一次用户输入引发的执行尝试。点击方框停止的只是当前 Turn。
- Goal 对应单意图或多意图中的一个独立目标。
- Node 是可信 Skill 模板编译出的可执行步骤。

`intent` 是能力类别，不是 Goal 主键。同一个 Run 内可以有多个相同 intent 的 Goal（例如分别查询南京和上海的历史），它们必须使用不同的 `goal_id`。Node 通过 `goal_id` 归属 Goal；子 Agent 只提交带 `goal_id` 的 `TaskResult`，不能直接修改 Goal 或 Run 状态。

Skill 的 `pause` 只声明哪个节点可以等待用户及判定字段，它不是状态仓库。节点需要补充信息时，只冻结该 Goal 和依赖它的下游；其他独立 Goal 继续运行并提交。短补全只送入当前 `focused_goal_id`，不会广播给其他等待 Goal。等待期间可以回答旁支问题，旁支完成后焦点必须回到仍在等待的 Goal。后续补充或“继续”会新建 Turn，从已提交节点之后恢复；当时正在运行的节点标为 `INTERRUPTED`，使用不变的 `operation_id` 幂等重试。

状态快照使用 schema v2 JSONB。状态更新只发生在 durable boundary：Run/Turn 创建、Node 开始、Node 提交、进入等待、请求中断和 Turn 结束。文件后端用原子替换服务开发测试；PostgreSQL 使用行锁、`(run_id, request_id)` 唯一约束及活动 Run 唯一索引保障幂等和并发。

Goal 状态由状态机根据所属 Node 统一推导：失败节点或依赖失败造成的跳过为 `FAILED`；存在等待节点为 `WAITING_USER`；存在中断节点为 `INTERRUPTED`；全部成功或普通跳过才是 `SUCCEEDED`。可降级 Goal 失败可以生成 error section 且 Run 结束为 `COMPLETED`；声明 `abort` 的硬失败才使 Run 为 `FAILED`。

`answer_delivered` 独立记录每个 Goal 是否已经向用户交付。暂停前已经完成但尚未展示的天气/制度结果，会在主 Goal 恢复后一起回答；等待期间已经单独回答过的旁支 Goal，不会在最终答案里重复出现。

前端停止按钮先请求状态转为 `INTERRUPTING`。执行器不再启动新节点，给当前节点最多 3 秒收尾，然后取消并持久化为 `INTERRUPTED`。`WAITING_USER`/`INTERRUPTED` 默认保留 30 天，终态默认保留 90 天。

## 核心不变式

- 意图识别输出不是执行计划。
- LLM 只能提出语义任务，不能绑定 Agent 或工具。
- 可执行节点只能由可信 Skill 声明编译产生。
- 每个执行节点使用自己的 scoped query。
- `goal_id` 与 `intent` 分离；同意图多 Goal 不合并状态或答案。
- DAG 先由显式边决定可执行性，priority 不表达依赖。
- 单意图、多意图和工作流共用同一 DAG、失败和暂停语义。
- 快速路由不能截断复合请求，短补全只能恢复当前等待 Goal。
- 已提交 Node 可重放，副作用由稳定 `operation_id` 保证幂等。
- 聚合层整理结果，但不伪造来源或改变执行状态。
