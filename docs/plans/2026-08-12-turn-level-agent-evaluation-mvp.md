# Hommey Turn 级 Agent 测评系统 MVP 改造计划

> 计划日期：2026-08-12
>
> 状态：待实施
>
> 本文性质：架构与实施计划；本轮只新增计划文档，不修改生产代码
>
> 首期范围：真实用户单轮对话的异步后处理测评（Turn Evaluation）。Session 级、多轮模拟用户和自动发布门禁留到后续阶段。

## 1. 结论先行

第一版采用“主流程旁路观测 + 独立 Judge Agent 后处理”的设计：

- 新增一个非用户意图的 `evaluate-turn` Skill，以及与其一一对应的 `turn_evaluator` Judge Agent。
- `evaluate-turn` **不声明 `intent`、不进入意图目录、不进入多意图 DAG、不出现在用户侧进度和 AnswerDocument 中**。
- 主程序完成一次 Turn、保存助手回答后，只生成一份有界的 `TurnEvaluationMetadata` 并尝试非阻塞投递；不在请求内调用 Judge。
- 独立 Evaluation Worker 读取 metadata，先抽取评测事实并执行确定性规则，再按策略调用 Judge Agent。
- 评测结果只写入独立评测表，不修改对话、记忆、行程、编排状态和 Skill 结果。
- 第一版只做 shadow evaluation：结果用于观察、人工复核和形成回归案例，不能直接改变线上回答，也不能自动修改 Agent 或 Prompt。

目标链路：

```text
用户请求
  ↓
Hommey 主流程
  ├─ 意图识别
  ├─ 多意图 DAG / Skill 执行
  ├─ RAG / 工具 / 记忆
  ├─ 生成 AnswerDocument
  └─ 保存 user / assistant 消息
  ↓
立即向用户返回
  │
  └─ put_nowait(TurnEvaluationMetadata)
                      ↓
             Evaluation Event Writer
                      ↓
              evaluation_subjects
              evaluation_runs(pending)
                      ↓
             独立 Evaluation Worker
                      ↓
        事实抽取 → 规则检查 → Judge Agent
                      ↓
              evaluation_runs(result)
                      ↓
          报表 / 告警 / 人工复核 / Golden Set
```

## 2. 目标与非目标

### 2.1 MVP 目标

1. 对一次已完成或已暂停等待用户的 Turn 做可重复的质量评测。
2. 能把评测失败定位到意图、Skill、Agent、RAG、编排或展示层，而不只产出一个总分。
3. Judge 不增加用户可感知延迟，Judge 故障不影响主程序可用性。
4. 保存产生回答和产生评分的双侧版本指纹，支持以后重评和版本对比。
5. 把人工确认的真实失败安全地沉淀为离线 Golden Case，形成升级闭环。
6. 数据模型为 Session 级评测、多 Judge、模拟用户、A/B 和发布门禁预留扩展点。

### 2.2 MVP 非目标

- 不在主请求内同步等待 Judge。
- 不评价逐个流式 chunk，只评价完整 Turn。
- 不让 Judge 重新调用业务 Skill、RAG、MCP、记忆或外部工具。
- 不让 Judge 修改主流程状态或向用户追加消息。
- 不自动根据 Judge 评语修改 Prompt、代码或知识库。
- 不把线上无标准答案的 Judge 分数直接作为唯一发布门禁。
- 不在第一版建设完整标注平台和复杂 BI 平台。
- 不在第一版实现多轮模拟用户；先把真实 Turn 的采集、评判、保存和回归闭环打通。

## 3. Judge Agent 与 Skill 的定位

### 3.1 目录与命名

计划新增：

```text
.agents/skills/evaluate-turn/
├── SKILL.md
├── hommey.yaml
├── schemas/
│   ├── input.json
│   └── output.json
└── script/
    └── agent.py
```

建议命名：

- Skill：`evaluate-turn`
- Agent：`turn_evaluator`
- 输入契约：`eval.turn.input.1`
- 输出契约：`eval.turn.result.1`

### 3.2 `hommey.yaml` 关键约束

示意配置：

```yaml
version: 1.0.0
display_name: Turn 质量评测
category: capability
agent_name: turn_evaluator
user_facing: false
enabled_by_default: true
risk_level: low
input_schema: schemas/input.json
output_schema: schemas/output.json
```

必须满足：

- **不声明 `intent`**：因此不会被 `core.intent_catalog` 收录。
- **不声明 `execution`**：因此不会被 `TaskGraphBuilder` 编译为用户任务步骤。
- `user_facing: false`：管理端若展示用户可用 Skill，必须过滤该 Skill。
- 不声明 `memory_hooks`、`pause`、`answer`、`side_effect_allowed`。
- 不授予 `rag_retrieval`、`memory`、`mcp`、`web_search` 等业务工具。
- 输入证据由评测 metadata 提供，Judge 不得自行获取“另一份现场”。

现有 `HommeySkillConfig` 只对“声明了 `intent` 的 Skill”强制要求 `agent_name + execution`，所以“有 Agent、无 intent、无 execution”的后处理 Skill 与当前契约兼容。现有 `LazyAgentRegistry` 也能按 `agent_name` 加载它。

### 3.3 与多意图系统的隔离契约

`evaluate-turn` 必须通过以下测试锁死边界：

```text
evaluate-turn 不在 SKILL_INTENTS
evaluate-turn 不在 GET /api/intents
turn_evaluator 不会由 supports_task_pipeline 选中
turn_evaluator 不出现在 PipelineOutput.results
turn_evaluator 不出现在返回给用户的 agents 列表
turn_evaluator 不发出用户侧 progress event
turn_evaluator 不生成 AnswerDocument section
```

Evaluation Worker 可以通过独立的 Lazy Registry 显式加载：

```text
evaluation_registry["turn_evaluator"]
```

但主编排器不能通过 intent、dependency 或 execution template 调用它。

### 3.4 独立运行时

Evaluation Worker 单独构造 Judge 运行环境：

- 使用独立 Judge 模型配置。
- 使用独立 LLM Client、并发限制、超时、熔断和调用额度。
- 不注入主程序的 `memory_manager` 和 `mcp_manager`。
- 不复用主请求的 `ExecutionBudget`。
- Judge Agent 只接收经过 schema 校验和脱敏的 metadata。

这既保留“Agent 对应 Skill”的统一插件形态，又避免把评测 Agent 变成业务 Agent。

## 4. Turn metadata 设计

### 4.1 设计原则

`TurnEvaluationMetadata` 是一次 Turn 的不可变评测快照，负责回答：

1. 用户本轮想做什么？
2. 主流程识别成了什么？
3. 调用了哪些 Skill/Agent，执行结果如何？
4. 当前 Turn 是完成、等待用户、降级、失败还是中断？
5. 最终回答是什么？
6. 政策性结论使用了哪些证据？
7. 哪个代码、模型、Prompt、Skill 和索引版本产生了该结果？

metadata 不保存模型隐式思维过程，不把日志全文直接交给 Judge，不默认携带用户身份信息。

### 4.2 MVP 输入契约

建议结构：

```json
{
  "schema_version": "eval.turn.input.1",
  "subject": {
    "request_id": "...",
    "session_id": "...",
    "run_id": "...",
    "turn_id": "...",
    "occurred_at": "..."
  },
  "conversation": {
    "user_message": "...",
    "assistant_message": "...",
    "previous_messages": []
  },
  "routing": {
    "intents": [],
    "selected_skills": []
  },
  "execution": {
    "terminal_state": "completed",
    "paused": false,
    "interrupted": false,
    "tasks": [],
    "agent_results": [],
    "timings": {},
    "error_codes": []
  },
  "answer": {
    "answer_document": null,
    "presentation_document": null,
    "sources": []
  },
  "evidence": {
    "retrieval_trace_ids": [],
    "items": []
  },
  "versions": {
    "git_revision": "",
    "production_model": "",
    "intent_prompt_version": "",
    "composer_prompt_version": "",
    "skill_versions": {},
    "rag_index_version": ""
  }
}
```

### 4.3 上下文边界

第一版只传：

- 本轮用户消息与助手完整回答。
- 最多最近 4～6 条必要上下文消息。
- 本轮意图、Skill、TaskResult 摘要。
- 本轮引用的证据，而不是知识库全文。
- 与本轮相关的行程事实和缺失字段，不传完整长期记忆。

上下文必须在该 Turn 的助手消息落库后立即冻结。Evaluation Worker 只能读取这份不可变快照，**不得在开始评测时按 `session_id` 查询“最新几条消息”**。否则用户在 Judge 运行期间继续提问时，较早 Turn 可能读到后续问题，形成“未来消息污染”。

例如用户连续发送 A、B、C 三轮时，应形成三份彼此独立的输入：

```text
Judge(A) = evaluate(snapshot_A)
Judge(B) = evaluate(snapshot_B)
Judge(C) = evaluate(snapshot_C)
```

三者允许并行和乱序完成；报表按 Turn 的 `occurred_at` / message sequence 排序，而不是按 Judge 完成时间排序。Judge Agent 不持有跨调用会话记忆，也不把 A 的评分结果自动加入 B 的输入。

必须设置字符、列表项和证据数量上限，metadata 超限时截断并记录：

```text
metadata_truncated = true
truncated_fields = [...]
```

Judge 看到信息不足时必须返回 `insufficient_evaluation_context`，不能用常识补齐主流程没有提供的证据。

### 4.4 证据快照

政策问答和合规检查至少保存：

```json
{
  "trace_id": "...",
  "chunk_id": "...",
  "chunk_hash": "...",
  "document_id": "...",
  "document_version": "...",
  "file": "...",
  "page": 3,
  "excerpt": "..."
}
```

只保存 `trace_id` 不足以长期复核，因为索引和原文会更新；只复制全文又会放大隐私和存储风险。因此保存可定位标识、版本、hash 和有界摘录。

### 4.5 生命周期语义

`terminal_state` 至少区分：

```text
completed
waiting_user
degraded
failed
interrupted
idempotent_replay
```

Judge 必须按状态采用不同预期：

- `completed`：评估是否完成本轮目标。
- `waiting_user`：不因“没有最终方案”扣分，评估追问是否必要、准确且没有重复询问。
- `degraded`：评估降级说明是否透明、剩余结果是否仍有用。
- `failed`：主要记录系统可靠性问题，通常无需调用 LLM。
- `interrupted`：不做回答质量评分，只记录中断状态。
- `idempotent_replay`：跳过重复评测，复用原 request 的结果。

## 5. metadata 的捕获与投递

### 5.1 捕获点

在 `HommeyWebInstance.process_message()` 创建一个请求级、纯内存的 `TurnEvaluationCollector`。主流程在已有边界把信息写入 Collector：

```text
输入规范化后       → user message / attachment summary
意图识别后         → routing / intents
任务管线执行后     → tasks / TaskResult / evidence
回答生成后         → answer/presentation document / timings
助手消息落库后     → terminal state / completed timestamp
```

Collector 只构造 DTO，不执行网络、数据库或 LLM 调用。普通 Chat 和 Stream Chat 都通过 `process_message()`，因此只需一个成功出口，不在 Route 和各 Agent 中重复植入评测调用。

第一阶段不要求所有字段一次性齐全；新字段应为可选，按版本逐步补充。禁止为了填满评测 metadata 改变业务决策或额外调用模型。

### 5.2 非阻塞投递

助手消息成功保存后执行：

```text
evaluation_sink.try_emit(metadata)
```

`try_emit` 的约束：

- 使用有界进程内队列的 `put_nowait`。
- 不同步调用 Judge。
- 不同步等待 PostgreSQL、Redis 或外部消息系统。
- 不允许异常向主流程传播。
- 队列满时增加 `evaluation_event_dropped_total`，随后放弃实时投递。

主程序返回值必须在启用和禁用评测时保持字节级等价。

### 5.3 异步写入与补偿

进程内 Event Writer 异步把 metadata 写入 PostgreSQL，并创建 pending evaluation run。它使用独立小连接池、短超时和有限重试。

因为进程可能在回答返回后、事件落库前退出，增加低频 Reconciler：

```text
扫描已有 assistant 消息
  ↓
按 request_id 查找缺失的 evaluation_subject
  ↓
用已持久化消息和 AnswerDocument 构造 reduced metadata
  ↓
补建 pending evaluation
```

补偿快照可能缺少完整 TaskResult/evidence，必须标记：

```text
capture_mode = reconciled
evaluation_context_quality = reduced
```

不能为了追求评测事件零丢失，把评测写入失败升级成聊天失败。

## 6. 数据存储

MVP 使用 PostgreSQL 独立评测表，不复用 `conversation_messages` metadata，也不把 Judge 当成普通 `skill_execution_runs`。

### 6.1 `evaluation_subjects`

保存不可变评测快照：

```text
subject_id              UUID PK
subject_type            turn / session
request_id
session_id
run_id
turn_id
capture_mode            live / reconciled / offline
schema_version
payload                 JSONB
producer_versions       JSONB
payload_hash
created_at
```

建议唯一约束：

```text
(subject_type, request_id)
```

对 Turn subject，`request_id` 是一次用户发送的稳定身份：同一次发送的网络重试必须复用原 `request_id`，新的用户问题必须生成新的 `request_id`。`payload_hash` 只用于完整性校验和发现异常差异，不参与 Turn 幂等键；否则实时捕获与补偿捕获产生的 payload 略有不同，就可能为同一 Turn 建立两份 subject。

实时 Event Writer 与 Reconciler 都使用：

```sql
INSERT ... ON CONFLICT (subject_type, request_id) DO NOTHING
```

先成功写入的不可变快照成为该 Turn 的事实源。Reconciler 只补缺，不覆盖实时快照。若未来确实需要重新采集同一个 Turn，必须显式增加 `subject_revision` 并生成新 subject，不能依赖 payload 变化隐式制造重复记录。

### 6.2 `evaluation_runs`

同时承担异步任务状态和版本化评测结果：

```text
evaluation_id           UUID PK
subject_id              FK
status                  pending / running / completed / failed / skipped
evaluator_version
judge_model
judge_prompt_version
rubric_version
verdict                 pass / warning / fail / critical_fail / unscored
score
dimension_scores        JSONB
reason_codes            JSONB
critical_errors         JSONB
rule_results            JSONB
explanation
review_required
attempts
lease_owner
lease_expires_at
token_usage             JSONB
latency_ms
error_code
created_at
started_at
completed_at
```

唯一约束：

```text
(subject_id, evaluator_version)
```

Judge 或 rubric 升级后创建新 `evaluator_version`，重评同一 subject；禁止覆盖旧评分。

### 6.3 连续提问下的幂等与并发控制

连续提问本身不是幂等冲突。一个 Session 内的每次新发送都有独立 `request_id`，所以 A、B、C 三轮可以产生三个独立评测任务，并发执行：

```text
session S / request A → subject A → evaluation A
session S / request B → subject B → evaluation B
session S / request C → subject C → evaluation C
```

必须在四层分别建立幂等和执行权约束：

| 层级 | 身份或约束 | 作用 |
|---|---|---|
| 用户一次发送 | `request_id` | 网络重试复用；新问题换新 ID |
| Turn 快照 | `UNIQUE(subject_type, request_id)` | 防 Event Writer、重试和 Reconciler 重复建 subject |
| 某版本评测 | `UNIQUE(subject_id, evaluator_version)` | 防同一 Judge/rubric 版本重复建 run |
| Worker 执行权 | `evaluation_id + lease_owner + lease_expires_at` | 防多个 Worker 同时写同一结果 |

Worker 领取任务时必须在同一数据库事务内使用：

```sql
SELECT evaluation_id
FROM evaluation_runs
WHERE status = 'pending'
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT :batch_size;
```

随后立即执行 `pending → running`，写入 `lease_owner`、`lease_expires_at` 并增加 `attempts`。Worker 崩溃后，只有租约过期的任务可以被恢复为 `pending`；正常运行的任务不能被其他 Worker 重复领取。

结果写入必须带所有权和状态条件：

```sql
UPDATE evaluation_runs
SET status = 'completed', ...
WHERE evaluation_id = :evaluation_id
  AND status = 'running'
  AND lease_owner = :worker_id;
```

这样旧 Worker 在超时后恢复，也不能覆盖新 Worker 已写入的结果。Judge 调用可能因超时被重复执行一次，但数据库中的有效结果仍保持 exactly-once effect；系统不承诺外部 LLM 调用本身 exactly once。

主评测流程不得对整个 `session_id` 加串行锁。Turn A、B、C 的 Judge 可以并行，队列积压只影响评测完成时间，不应阻止用户继续聊天。只有未来的 Session Judge 才需要按会话终态构造独立 subject。

未来 Session 级评测采用另一套身份：

```text
subject_type = session
session_id
session_revision = 触发评测时最后一条消息的 sequence_no
evaluation_trigger = completed / cancelled / idle_timeout
```

其推荐唯一键为：

```text
(subject_type, session_id, session_revision)
```

同一 Session 在第 6 轮结束后评测，之后恢复到第 10 轮再次结束时，可以产生两个明确版本的 Session subject；它不能复用 Turn 的 `request_id` 幂等规则。

### 6.4 `evaluation_reviews`

保存人工复核，不覆盖 Judge：

```text
review_id
evaluation_id
reviewer
human_verdict
human_reason_codes       JSONB
agrees_with_judge
comment
created_at
```

### 6.5 数据保留与权限

- 默认不把明文 `user_id` 发送给 Judge；存储侧使用受控关联或不可逆 subject key。
- 沿用项目现有敏感信息脱敏规则，并为评测补充专门的 payload sanitizer。
- 报表默认展示聚合指标和脱敏摘要；查看原始对话需要管理员权限并留审计记录。
- 原始 metadata、Judge 原始输出和人工评语分别配置保留期。
- 日志只写 ID、状态和错误码，不打印完整对话和证据。

## 7. 评测执行流程

### 7.1 阶段一：资格判断

第一版建议：

| 场景 | 策略 |
|---|---|
| `ask-question` 政策问答 | 100% 规则 + Judge |
| `check-trip-compliance` 合规检查 | 100% 规则 + Judge |
| `plan-trip` 最终方案 | 100% 规则 + Judge |
| 无证据、降级或用户点踩 | 100% |
| Agent/工具明确失败 | 100% 规则，通常不调用 Judge |
| `waiting_user` 信息收集轮 | 20% 抽样，后续按需要提高 |
| preference / memory-query | 规则为主，低比例 Judge |
| chitchat | 跳过 |
| interrupted / idempotent replay | 跳过 |

采样决定必须可复现，例如根据 `request_id` hash，而不是每次随机；同一个 Turn 重跑时应得到相同的入选结果。

### 7.2 阶段二：关键信息抽取

在调用 Judge Agent 前，由 `evaluate-turn` 的执行层把原始 metadata 规范化为 `TurnEvaluationFacts`。

尽量使用确定性 Python 逻辑抽取：

```text
primary_intent
selected_skills
successful_agents / failed_agents
terminal_state
user_goal
answer_kind
required_fields / present_fields / missing_fields
policy_claim_candidates
source_count
evidence_items
error_codes
total_latency
metadata_completeness
```

其中：

- intent、Skill、执行状态、缺失字段、来源数量和耗时直接从结构化 metadata 提取。
- LLM 只负责语义性任务，例如识别回答中的主要政策 claim、判断是否真正回应用户、判断表达是否可执行。
- 抽取后的 facts 与原始有界内容一起交给 Judge，避免 Judge 在大段无关 metadata 中自行寻找事实。
- 抽取失败不调用 Judge，记录 `EVALUATION_FACT_EXTRACTION_FAILED`。

### 7.3 阶段三：确定性规则

首期规则包括：

```text
ANSWER_SCHEMA_INVALID
AGENT_EXECUTION_ERROR
REQUEST_TIMEOUT
SOURCE_MISSING
EVIDENCE_NOT_LINKED
REQUIRED_SLOT_MISSING
REPEATED_REQUIRED_SLOT_QUESTION
SHOULD_RETURN_UNKNOWN
SENSITIVE_DATA_RISK
METADATA_INCOMPLETE
```

规则优先于 Judge：

- 隐私泄漏、跨用户数据、无证据却给出确定政策金额等直接 `critical_fail`。
- 确定的结构或执行错误不能被 Judge 的高分抵消。
- 已经能够确定为系统错误的 Turn 可以跳过 LLM，降低成本。

### 7.4 阶段四：Judge Agent

MVP 只评分五个维度，每项 0～4：

| 维度 | 定义 |
|---|---|
| understanding | 是否正确理解本轮用户目标 |
| task_progress | 是否在当前生命周期状态下正确完成或推进任务 |
| groundedness | 政策和合规结论是否得到输入证据支持 |
| safety | 是否避免越权、隐私泄漏和无依据的确定性断言 |
| clarity | 是否清晰、简洁且让用户知道下一步 |

Judge 必须遵守：

1. 只根据输入 metadata 和 evidence 评判，不使用模型自身掌握的公司政策。
2. `waiting_user` 的正确追问可以获得高分。
3. 没有提供足够证据时返回上下文不足，不猜测政策是否正确。
4. 每个失败 reason code 必须引用对应的输入字段、回答片段或 evidence id。
5. 输出严格符合 JSON Schema；解析失败最多重试一次。

建议输出：

```json
{
  "schema_version": "eval.turn.result.1",
  "verdict": "warning",
  "score": 78,
  "dimensions": {
    "understanding": 4,
    "task_progress": 4,
    "groundedness": 2,
    "safety": 4,
    "clarity": 3
  },
  "reason_codes": ["CITATION_DETAIL_INCOMPLETE"],
  "critical_errors": [],
  "findings": [
    {
      "code": "CITATION_DETAIL_INCOMPLETE",
      "severity": "medium",
      "message": "结论有证据支持，但回答没有展示可核验的页码。",
      "subject_ref": "answer.sources[0]",
      "evidence_refs": ["chunk_xxx"]
    }
  ],
  "review_required": false,
  "summary": "回答基本正确，但证据展示不完整。"
}
```

### 7.5 阶段五：最终裁决

由确定性 Decision Engine 合并规则和 Judge 结果：

```text
命中 critical rule       → critical_fail
命中普通 hard rule       → 至少 fail
只有 soft findings       → 结合 Judge 得分得到 warning/pass
Judge 不可用             → unscored，不把系统回答判成 fail
metadata 明显不足        → unscored + review_required
```

最终裁决逻辑必须是普通代码，不再调用第二个模型。

## 8. 结果如何指导 Agent 升级

### 8.1 稳定错误码与责任映射

| Reason code | 首要责任模块 |
|---|---|
| `INTENT_MISROUTED` | Intention Agent / intent router |
| `SKILL_SELECTION_WRONG` | Skill 描述 / TaskGraphBuilder |
| `REQUIRED_SLOT_MISSING` | event-collection / trip intake |
| `REPEATED_REQUIRED_SLOT_QUESTION` | context / active trip / TurnResolver |
| `TASK_NOT_COMPLETED` | 对应业务 Agent / orchestration |
| `RETRIEVAL_MISS` | RAG parser / chunker / retrieval / reranker |
| `POLICY_CLAIM_UNSUPPORTED` | ask-question / compliance evidence gate |
| `CITATION_MISMATCH` | RAG source serialization / composer |
| `SHOULD_RETURN_UNKNOWN` | ask-question / check-trip-compliance |
| `MEMORY_NOT_USED` | memory-query / context assembly |
| `CROSS_USER_DATA_RISK` | memory repository / authorization |
| `TOOL_CALL_FAILED` | MCP / timeout / retry / failure policy |
| `RESPONSE_NOT_ACTIONABLE` | composer / AnswerDocument |

每条评测必须绑定生产侧版本指纹：

```text
git revision
production model
prompt versions
skill versions
RAG index / schema / embedding versions
```

没有版本指纹的评分不能进入版本对比结论。

### 8.2 线上到回归的闭环

```text
线上评测发现失败
  ↓
按 reason_code / Skill / 版本聚类
  ↓
开发或政策人员查看证据并人工确认
  ↓
脱敏后导出为候选 Golden Case
  ↓
补充 expected intent / skill / facts / evidence / terminal state
  ↓
旧版本与候选版本离线回放
  ↓
比较质量、延迟、成本和严重错误
  ↓
人工决定是否发布
```

禁止把 Judge 的自然语言建议直接写回 Agent Prompt。自动化负责发现、聚类和回放，修改决策仍由开发人员完成。

### 8.3 开发可见性

MVP 先提供简单报表，不把管理 UI 作为上线前置：

- 每日/按需生成 Markdown 或 JSON 汇总。
- 支持按时间、Skill、Agent、verdict、reason code 和版本筛选。
- 列出严重失败和需要人工复核的 subject 链接。
- 展示各 Skill 通过率、严重错误率、Judge 失败率、评测覆盖率和队列积压。

后续管理页至少包含：

```text
总览趋势
Skill / Agent 分项
错误类型分布
版本前后对比
失败 Turn 详情
人工复核队列
```

失败详情按以下顺序展示：

```text
用户输入 → 上下文 → intent → skills/tasks → evidence → 最终回答
        → 规则结果 → Judge findings → 生产/Judge 版本
```

### 8.4 告警

首期只对高价值事件告警：

- `critical_fail`
- `CROSS_USER_DATA_RISK`
- `POLICY_CLAIM_UNSUPPORTED`
- 某 Agent/Skill 的失败率相对基线显著上升
- Judge Worker 持续失败或队列积压超过阈值

普通低分进入日报，避免告警疲劳。

## 9. 可观测性与运行指标

至少记录：

```text
evaluation_events_emitted_total
evaluation_event_dropped_total
evaluation_queue_depth
evaluation_runs_total{status, verdict, skill}
evaluation_rule_failures_total{reason_code}
evaluation_judge_latency_ms
evaluation_judge_tokens_total
evaluation_judge_errors_total{error_code}
evaluation_reconciled_total
evaluation_review_agreement_rate
```

不得把 `request_id`、Agent 名称等高基数字段作为 metrics label；它们只进入结构化日志或评测表。

## 10. 配置建议

计划增加独立配置组：

```text
HOMMEY_EVALUATION_ENABLED=false
HOMMEY_EVALUATION_LLM_ENABLED=false
HOMMEY_EVALUATION_SAMPLE_RATE=0.2
HOMMEY_EVALUATION_QUEUE_SIZE=256
HOMMEY_EVALUATION_WORKER_CONCURRENCY=2
HOMMEY_EVALUATION_JUDGE_TIMEOUT_SEC=30
HOMMEY_EVALUATION_JUDGE_MODEL=...
HOMMEY_EVALUATION_CONTEXT_MESSAGES=4
HOMMEY_EVALUATION_RETENTION_DAYS=30
```

默认关闭；按“只采集 → 规则 → LLM Judge”的顺序逐级开启。采样策略后续可由按 Skill 风险分级替代全局比例。

## 11. 计划修改范围

以下只是预计文件边界，实施时按现有项目结构复核：

### 11.1 新增

```text
evaluation/models.py                 输入、事实、结果 DTO
evaluation/collector.py              请求级 metadata 收集器
evaluation/sink.py                   Noop / bounded async sink
evaluation/repository.py             独立评测表访问
evaluation/rules.py                  确定性规则
evaluation/decision.py               规则 + Judge 最终裁决
evaluation/worker.py                 pending run worker
evaluation/reconciler.py             丢事件补偿
evaluation/report.py                 MVP 汇总报告

.agents/skills/evaluate-turn/SKILL.md
.agents/skills/evaluate-turn/hommey.yaml
.agents/skills/evaluate-turn/schemas/input.json
.agents/skills/evaluate-turn/schemas/output.json
.agents/skills/evaluate-turn/script/agent.py

scripts/run_evaluation_worker.py
webui_new/auth/migrations/<next>_agent_evaluation.sql
```

### 11.2 修改

```text
webui_new/manager.py                 创建 Collector、成功后 try_emit
settings.py                          独立评测配置
.env.example                         配置说明
utils/observability.py               评测运行指标
webui_new/skill_platform/service.py  用户侧/管理侧正确处理 user_facing=false
tests/test_skill_registry.py         加入后处理 Skill，锁定非意图边界
```

不修改各业务 Agent 的判断逻辑，不在每个业务 Skill 中添加 Judge 调用。

## 12. 分阶段实施

### 阶段 0：Rubric 与契约冻结

1. 定义 input/output JSON Schema、reason code、五维评分和 hard rules。
2. 人工制作 30～50 个 Turn 样本，覆盖 pass、waiting_user、unsupported claim、引用错误、重复追问和系统失败。
3. 由至少一名开发人员和一名业务/政策人员标注关键样本。
4. 冻结 `eval.turn.input.1`、`eval.turn.result.1` 和 `travel-rubric-v1`。

验收：相同输入能稳定得到结构合法结果；严重错误口径有人工共识。

### 阶段 1：只采集，不评分

1. 实现 Collector、Noop/Async Sink、评测表和 Reconciler。
2. `EVALUATION_ENABLED=true`，`EVALUATION_LLM_ENABLED=false`。
3. 验证 metadata 完整度、脱敏、Turn 唯一键、队列丢弃和补偿。
4. 验证 Event Writer 与 Reconciler 竞争时，同一 `request_id` 只产生一个 subject。
5. 比较开关前后聊天响应内容和 P50/P95。

验收：主回答行为完全一致；评测组件全部不可用时聊天仍正常；有效 Turn 可被异步捕获或补偿。

### 阶段 2：规则检查 + Judge Shadow

1. 新增 `evaluate-turn` Skill 和 `turn_evaluator` Agent。
2. 实现 facts 抽取、规则检查、Judge 调用和最终裁决。
3. 实现 `SKIP LOCKED` 任务领取、Worker 租约、过期恢复和带所有权的条件写入。
4. 先对 30～50 个已标注样本校准 Judge，再对核心 Skill 小流量开启。
5. Judge 结果只存储，不告知用户、不影响发布。

验收：JSON Schema 通过率达到约定目标；critical rule 无法被 Judge 覆盖；Judge 故障不传播到聊天。

### 阶段 3：开发可见性与人工复核

1. 增加日报/周报和失败详情查询。
2. 增加人工复核记录。
3. 统计 Judge 与人工在 verdict、critical error、reason code 上的一致率。
4. 建立按 Skill、版本、错误类型的趋势视图和高风险告警。

验收：开发人员能从聚合指标下钻到脱敏 Turn、证据和责任模块。

### 阶段 4：Golden 回归与 Session 级扩展

1. 人工确认的失败样本经过脱敏后加入 Golden Set。
2. 建立旧版本与候选版本的离线批量回放。
3. 在 Turn 契约上扩展 `subject_type=session`，新增 Session terminal trigger 和多轮 rubric。
4. 只有在 Judge 经人工校准后，才把部分稳定指标升级为发布参考或门禁。

验收：一次 Agent/Prompt/RAG/Skill 改动能够给出版本前后质量、延迟和成本对比；Session 中的 `waiting_user` 不会被误判为未完成。

## 13. 非干扰验收清单

上线 Judge Shadow 前必须全部通过：

- [ ] 评测关闭时，生产行为与改造前一致。
- [ ] 开关评测后，同一 mock 模型输入得到完全相同的用户响应和 AnswerDocument。
- [ ] Judge 超时、抛异常、返回非法 JSON 时，Chat 仍成功。
- [ ] Evaluation PostgreSQL 不可用时，Chat 仍成功。
- [ ] 队列满时只增加 dropped metric，不阻塞请求。
- [ ] Stream Chat 不逐 chunk 评测，完整 Turn 最多创建一个 subject。
- [ ] `request_id` 重放不产生重复 evaluation run。
- [ ] Event Writer 与 Reconciler 并发处理同一 `request_id` 时只产生一个 subject。
- [ ] 两个 Worker 并发领取时，同一 evaluation run 只有一个有效 lease owner。
- [ ] Worker 租约过期重跑后，旧 Worker 不能覆盖新 Worker 的结果。
- [ ] 用户连续发送 A/B/C 时产生三个独立 subject，允许乱序完成且互不覆盖。
- [ ] Judge A 的快照不包含 A 完成后才产生的 B/C 消息。
- [ ] `turn_evaluator` 不进入意图 prompt、TaskGraphBuilder、用户进度或 Agent 列表。
- [ ] Judge 没有 memory/MCP/RAG 写权限，不会改变业务事实。
- [ ] Evaluation Worker 使用独立模型并发、预算和数据库连接池。
- [ ] 评测日志不包含完整对话、附件正文或敏感字段。
- [ ] 开启采集后的 Chat P95 增量不超过预先约定的小幅阈值；若超出则关闭开关并排查。

## 14. MVP 完成定义

满足以下条件才算最小闭环完成：

1. `evaluate-turn` 作为独立 Skill/Agent 可由 Evaluation Worker 加载，但无法由用户意图触发。
2. 核心业务 Turn 完成后能异步生成版本化 metadata，主请求不等待 Judge。
3. Worker 能抽取关键信息、执行规则、调用 Judge 并保存结构化结果。
4. 严重错误、普通错误、上下文不足和 Judge 系统失败有不同语义。
5. 开发人员能看到各 Skill 的覆盖率、通过率、严重错误率、Top reason codes 和失败详情。
6. 人工能复核 Judge，确认后的失败能安全转成 Golden Case。
7. Judge、数据库或队列故障经过注入测试，均不会改变主程序响应或业务状态。

完成以上 MVP 后，再扩展 Session 级、多轮模拟用户、多 Judge 共识、A/B 和发布门禁；无需推翻现有 Turn metadata、evaluation subject/run 和非意图 Judge Skill 的基础设计。
