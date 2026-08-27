# 意图与编排职责拆分 Phase 1 更新记录

> 日期：2026-08-25  
> 分支：`codex/intention-orchestration-v3`  
> 实施状态：Phase 1 代码完成，待真实模型与部署环境验证  
> 状态机版本：仍为 `WorkflowRunState schema_version=2`

## 1. 本阶段目标

本阶段只解决 `IntentionAgent` 承担语义识别、执行授权和 Query 拆解过多的问题：

```text
用户 Query + 受限上下文
  -> IntentionAgent: 识别并隔离 IntentGroup
  -> OrchestrationPolicy: 逐组授权
  -> TaskDecomposer: 确定性协议适配，不再调用 LLM
  -> TaskValidator: 按 group_id 校验
  -> TaskGraphBuilder: 根据可信 Skill 声明编译节点
  -> TaskExecutor -> 既有业务子 Agent
  -> AnswerComposer
  -> ExecutionLifecycle + OrchestrationStateStore 持久化状态
```

这里没有新增 “Child Agent”。child agent 仍指天气、制度、车次、行程规划等既有业务 Agent。新增的是无模型的 `OrchestrationPolicy` 组件，不是一个 LLM Agent。

## 2. 已完成

| 项目 | 状态 | 结果 |
| --- | --- | --- |
| 精简意图输出 | 已完成 | 新增 `IntentAnalysis(groups, relations)`；移除意图层执行授权和节点信息 |
| 多意图 Query 隔离 | 已完成 | 每个 `IntentGroup` 持有自己的 query、entities 和 source_refs |
| 相同意图多目标 | 已完成 | `group_id` 成为 Goal 身份；北京天气和上海天气不再按 intent 合并 |
| 执行授权外移 | 已完成 | `OrchestrationPolicy` 重新执行 guard、领域、可见性和置信度规则 |
| 移除二次 LLM 拆解 | 已完成 | `TaskDecomposer` 变为确定性适配器，不再发起模型调用 |
| 组级校验 | 已完成 | Validator 校验 `PolicyDecision.group_id` 与任务一一对应 |
| 实体隔离 | 已完成 | Executor 给每个节点注入该 Goal 自己的 `key_entities` |
| 旧协议兼容 | 已完成 | 可读取旧 `routing/intents/key_entities/rewritten_query`；授权标记会被重新计算 |
| 评估采集兼容 | 已完成 | 优先记录 `groups` 与已授权 `policy_decisions` |

## 3. 明确未完成

| 项目 | 状态 | 后续要求 |
| --- | --- | --- |
| `WorkflowRunState v3` | 未开始 | 设计并迁移 `plan.node_specs` 与 `runtime.node_states` |
| 数据库迁移 `0022` | 未开始 | v3 schema 确定后再新增；本阶段不改表 |
| 完整计划快照持久化 | 未完成 | 当前恢复仍按 Skill 声明重编译，并用 `graph_hash` 检查 |
| `OrchestrationRuntime` 收口 | 未开始 | manager、pipeline、lifecycle 的控制职责仍分布在现有模块 |
| `ChildAgentRunner` 重命名 | 未开始 | `OrchestrationAgent` 仍保留兼容名称 |
| 渐进式 Skill 解析 | 未完成 | 当前仍由 Skill catalog/manifest 一次性提供声明并由 GraphBuilder 编译 |
| 回答交付事务边界 | 未完成 | `answer_delivered` 与助手消息落库之间仍存在进程崩溃窗口 |
| 删除旧协议字段 | 未开始 | 等所有消费者改用新字段后再进行破坏性移除 |

以上未完成项不得在发布说明中标记为已上线。

## 4. 各组件输入与输出

| 组件 | 输入 | 输出 | 不负责 |
| --- | --- | --- | --- |
| `IntentionAgent` | 当前 Query、当前任务、受限会话上下文 | `IntentAnalysis` | Skill 授权、Agent 选择、执行步骤、状态写入 |
| `OrchestrationPolicy` | `IntentAnalysis`、可信原始 Query、受限上下文 | 每组 `PolicyDecision`、主意图、澄清信息 | 调用业务 Agent、修改状态 |
| `TaskDecomposer` | 意图组、策略决定 | 一组 `IntentTask` | LLM 拆解、Agent 绑定 |
| `TaskValidator` | `IntentTask`、策略信封 | 已验证语义任务 | 补写用户未表达的目标 |
| `TaskGraphBuilder` | 已验证任务、Skill manifest | 绑定可信 Agent 的 DAG 节点 | 识别用户意图 |
| `TaskExecutor` | DAG、scoped context、生命周期接口 | `TaskResult` | 直接修改 Run/Goal 状态 |
| 业务子 Agent | 单节点 query、组内实体、依赖结果 | 业务结果 | 选择下一个节点、聚合全局答案 |
| `AnswerComposer` | Goal、任务结果 | `AnswerDocument` | 改写节点执行状态 |
| `ExecutionLifecycle` | 执行事件 | 状态转换请求 | 数据库存储实现 |
| `OrchestrationStateStore` | 状态变更函数、预期 revision | 持久化 v2 快照 | 业务判断、意图识别 |

## 5. 多意图示例

用户输入：`查9月2日上海天气和餐补政策，再根据这些结果规划北京到上海两天的出差行程`

### 5.1 IntentionAgent 输出

```json
{
  "schema_version": 1,
  "groups": [
    {
      "group_id": "weather_shanghai",
      "intent": "information_query",
      "query": "查询2026年9月2日起上海两天的天气",
      "confidence": 0.95,
      "entities": {"destination": "上海", "date": "2026-09-02", "duration": 2},
      "source_refs": ["current_query"]
    },
    {
      "group_id": "meal_policy",
      "intent": "rag_knowledge",
      "query": "查询公司上海出差餐补政策",
      "confidence": 0.94,
      "entities": {"destination": "上海"},
      "source_refs": ["current_query"]
    },
    {
      "group_id": "trip_plan",
      "intent": "itinerary_planning",
      "query": "规划2026年9月2日起北京到上海两天的公司出差行程",
      "confidence": 0.96,
      "entities": {"origin": "北京", "destination": "上海", "date": "2026-09-02", "duration": 2},
      "source_refs": ["current_query"]
    }
  ],
  "relations": [
    {
      "from": ["weather_shanghai", "meal_policy"],
      "to": "trip_plan",
      "type": "required_context"
    }
  ]
}
```

### 5.2 策略和兼容信封

策略层在上述 JSON 上追加：

```json
{
  "original_query": "查9月2日上海天气和餐补政策，再根据这些结果规划北京到上海两天的出差行程",
  "policy_decisions": [
    {"group_id": "weather_shanghai", "intent": "information_query", "authorized": true, "reason_code": "AUTHORIZED", "skill": "query-info"},
    {"group_id": "meal_policy", "intent": "rag_knowledge", "authorized": true, "reason_code": "AUTHORIZED", "skill": "ask-question"},
    {"group_id": "trip_plan", "intent": "itinerary_planning", "authorized": true, "reason_code": "AUTHORIZED", "skill": "plan-trip"}
  ]
}
```

真实信封还暂时包含 `routing/intents/key_entities/rewritten_query`。这些字段只为旧调用方存在；发生实体冲突时，全局 `key_entities` 会省略冲突字段，执行器始终使用组内 `entities`。

### 5.3 编译与执行

三个授权组先一对一成为三个 Goal。天气和政策 Goal 可以并行；规划 Goal 依赖它们。随后 `TaskGraphBuilder` 才读取 Skill manifest，将规划 Goal 展开为内部工作流节点。LLM 输出的 relation 只能建立 Goal 间语义依赖，不能指定 Agent、工具、重试或失败策略。

## 6. 当前数据库事实

本阶段不需要数据库 DDL 适配。生产环境唯一可信来源仍为 PostgreSQL：

- `orchestration_runs.state JSONB`：完整 `WorkflowRunState v2` 快照。
- `orchestration_runs.revision`：乐观并发版本。
- `orchestration_turns`：Turn 幂等和生命周期记录。
- 文件状态后端：仅开发和测试。

当前 v2 快照示例：

```json
{
  "schema_version": 2,
  "run_id": "run_01",
  "user_id": "user_01",
  "session_id": "session_01",
  "revision": 5,
  "status": "ACTIVE",
  "current_turn_id": "turn_01",
  "current_request_id": "request_01",
  "current_goal_ids": ["weather_shanghai", "trip_plan"],
  "focused_goal_id": "trip_plan",
  "original_query": "用户原始查询",
  "intention_data": {
    "schema_version": 1,
    "groups": [
      {"group_id": "weather_shanghai", "intent": "information_query", "query": "查询上海天气", "confidence": 0.95, "entities": {"destination": "上海"}, "source_refs": ["current_query"]},
      {"group_id": "trip_plan", "intent": "itinerary_planning", "query": "根据天气规划北京到上海行程", "confidence": 0.96, "entities": {"origin": "北京", "destination": "上海"}, "source_refs": ["current_query"]}
    ],
    "relations": [{"from": ["weather_shanghai"], "to": "trip_plan", "type": "required_context"}],
    "policy_decisions": [
      {"group_id": "weather_shanghai", "intent": "information_query", "authorized": true, "reason_code": "AUTHORIZED", "skill": "query-info"},
      {"group_id": "trip_plan", "intent": "itinerary_planning", "authorized": true, "reason_code": "AUTHORIZED", "skill": "plan-trip"}
    ]
  },
  "semantic_tasks": [
    {"task_id": "weather_shanghai", "intent": "information_query", "query": "查询上海天气", "entities": {"destination": "上海"}, "depends_on": []},
    {"task_id": "trip_plan", "intent": "itinerary_planning", "query": "根据天气规划北京到上海行程", "entities": {"origin": "北京", "destination": "上海"}, "depends_on": ["weather_shanghai"]}
  ],
  "goals": {
    "weather_shanghai": {"goal_id": "weather_shanghai", "intent": "information_query", "status": "SUCCEEDED", "query": "查询上海天气", "answer_delivered": false},
    "trip_plan": {"goal_id": "trip_plan", "intent": "itinerary_planning", "status": "RUNNING", "query": "根据天气规划北京到上海行程", "answer_delivered": false}
  },
  "nodes": {
    "weather_shanghai-information_query": {"node_id": "weather_shanghai-information_query", "goal_id": "weather_shanghai", "status": "SUCCEEDED", "operation_id": "run_01:weather_shanghai-information_query", "attempts": 1, "result": {}, "error_code": null},
    "trip_plan-itinerary_planning": {"node_id": "trip_plan-itinerary_planning", "goal_id": "trip_plan", "status": "RUNNING", "operation_id": "run_01:trip_plan-itinerary_planning", "attempts": 1, "result": null, "error_code": null}
  },
  "waits": [],
  "graph_hash": "...",
  "skill_versions": {"query-info": "1.1.0", "plan-trip": "1.1.0"},
  "created_at": "2026-08-25T12:00:00+00:00",
  "updated_at": "2026-08-25T12:00:05+00:00",
  "expires_at": "2026-09-24T12:00:00+00:00"
}
```

`semantic_tasks` 和工作流展开节点在示例中省略了带默认值的展示字段及部分内部节点，数据库实际保存 Pydantic 完整序列化结果。这里仅有一个 `nodes`：它保存运行状态。当前 v2 也没有独立 `phase` 字段，执行阶段由 Run/Turn/Node 状态组合推导。之前讨论中出现两个同名 `nodes`，是把“静态节点规格”和“动态节点状态”错误拼进同一对象造成的示例冲突。未来 v3 应明确改为 `plan.node_specs` 与 `runtime.node_states`，不能出现两个同名 JSON key。

## 7. 状态流是否变化

本阶段改变的是状态机之前的输入构造，不改变状态枚举和持久化边界：

```text
IntentGroup
  -> PolicyDecision
  -> Goal/IntentTask
  -> Skill DAG Node
  -> READY -> RUNNING -> SUCCEEDED/FAILED/WAITING_USER/INTERRUPTED
  -> derive Goal status
  -> derive Run/Turn status
  -> StateStore 持久化 revision + 1
```

主要语义变化是 Goal 的身份改为 `group_id`，所以同一 intent 可以有多个独立 Goal；Node 仍由可信 Skill 编译，Agent 仍不能自行改状态。

## 8. 兼容与回滚边界

- 新入口：`groups + relations -> policy_decisions`。
- 旧入口：旧 envelope 先转换为 groups，再重新执行策略；旧 `should_call_skill=true` 不能绕过策略。
- 新输出仍带旧投影，便于 manager、评估和历史状态滚动升级。
- 本阶段没有 DDL，回滚代码不会遇到数据库 schema 不兼容。
- 已保存的旧 v2 `intention_data` 可在恢复时转换；恢复使用原始 Query 重新授权，不能用“继续”两个字替代原任务范围。

## 9. 验证状态

已通过意图契约、多意图、授权 guard、DAG、状态恢复、并发、记忆、评估和 Web 错误契约定向测试。排除已确认的外部环境和仓库既有失败后，完整回归结果为 `594 passed, 27 skipped, 6 deselected`。真实模型端到端对话、PostgreSQL 集成、Redis 集成和部署后多 worker 验证仍未执行，发布前必须补做。
