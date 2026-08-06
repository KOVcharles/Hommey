# 任务级多意图编排（Task-Scoped Orchestration V2）

## 概述

项目目前并存两套编排链路：

- **v1 `OrchestrationAgent`**（`agents/orchestration_agent.py`）：按 `agent_schedule` 的 priority 分批执行子智能体。多意图在调度计划层按 agent 去重合并，所有子智能体共享同一个 `context.rewritten_query`（整段改写后的原始 query）。
- **v2 `MultiIntentPipeline`**（`core/orchestration/`）：把多意图请求**分解成相互独立的语义任务**，每个任务持有独立的 scoped query，各自执行后统一整合成一张答案卡。去重被移到"意图 → 任务"边界，由校验层保证。

v2 解决的是 v1 的一个真实缺陷：**当 `rag_knowledge` 与 `information_query` 同时被调度时，v1 的 rag agent 会看到包含"天气"的整段 query，可能用制度检索去查天气**，产生跨意图的错误逻辑。v2 通过"每任务 scoped query + 越界校验"从机制上杜绝这类错误。

入口在 `webui_new/manager.py`：`ORCHESTRATION_V2_CONFIG["enabled"]`（默认 True）且 `supports_phase_one(intention_data)` 为真时走 v2，否则回退 v1。CLI 入口目前仍走 v1。

## 一期范围（当前实现）

一期只接管**恰好同时包含 `rag_knowledge`（制度/标准）与 `information_query`（天气/公开交通）**、且两者相互独立的请求。其他单意图、行程收集、行程规划、合规检查、写操作流程继续走 v1。

## 执行链路

```text
Intent recognition
  -> TaskDecomposer   (LLM 拆分为语义任务；失败用确定性 fallback)
  -> TaskValidator    (授权、scope、副作用、依赖边界校验)
  -> TaskGraphBuilder (可信 intent->agent 绑定，生成依赖批次)
  -> TaskExecutor     (批次并行执行，每任务 scoped context)
  -> AnswerComposer   (LLM 整合答案卡；失败用确定性 composer)
  -> AnswerDocument   (卡片 + 纯文本 + 来源)
```

核心不变式：**LLM 只能生成语义任务，不能选择 Agent 或工具**；Agent 绑定由应用代码按可信调度规则完成。每个执行节点 = 一个意图 = 一个 scoped query。

## 一期的窄点（6 层约束）

一期窄不只是入口的 `supports_phase_one` 一个判断，而是整条链路的嵌套约束：

| # | 层 | 位置 | 约束 |
|---|---|---|---|
| 1 | 入口闸门 | `validator.supports_phase_one` | `set(callable_intents) == {rag_knowledge, information_query}`，**恰好相等**；单意图、三意图组合均不进 v2 |
| 2 | 意图→步骤映射 | `graph_builder.compile` | 每个 intent 必须且只能映射**恰好 1 个** execution step；`itinerary_planning`（5 步）、`trip_compliance`（3 步）无法通过 |
| 3 | 依赖 + 副作用 | `validator` | 禁止 `depends_on`、禁止 `side_effect`；但 `graph_builder.batches()` 已实现 priority + depends_on 分批，执行机制就绪但被校验层禁用 |
| 4 | 跨意图 scope 规则 | `validator` | `_WEATHER_TERMS / _POLICY_TERMS / _POLICY_CATEGORIES` 是按 policy↔weather 配对手写死的规则 |
| 5 | 确定性 fallback | `decomposer.fallback` | 只认识 rag/info 两个意图，LLM 拆解失败时其他意图产出空列表 |
| 6 | Composer 三件套 | `composer` / `fallback_composer` / `answer_validator` | 只写、只校验 policy/weather 分区 |

**已预留的扩展面**：`AnswerSection.kind` 已声明 `memory / preference / trip / notice / general` 等取值，前端渲染与校验规则尚未覆盖，为二期拓宽做好了数据层准备。

## 二期计划（拓宽方向）

二期沿用一期不变式（每个执行节点 = 一个意图 = 一个 scoped query），按阶段渐进拓宽。每阶段入口放宽必须与第 4/5/6 层同步，否则 scoped 保证会被侵蚀，跨意图错误逻辑会复发。

### 阶段 0：放开入口（优先，低成本）

把第 1 层从"恰好相等"改为"子集判定"，允许集扩到所有**单步、只读、独立**的意图：

```python
PHASE_ONE_INTENTS = frozenset({
    "rag_knowledge", "information_query", "memory_query",
})
def supports_phase_one(intention_data):
    callable_set = set(callable_intents(intention_data))
    return bool(callable_set) and callable_set <= PHASE_ONE_INTENTS
```

立即覆盖的真实组合：`{rag_knowledge}` 单意图、"查补贴 + 查我去过哪"、`{rag, info, memory}` 三意图。同步改动：
- validator 给 `memory_query` 加 scope 规则（memory 任务 query 含 policy/weather 词 → 拒绝）；
- `decomposer.fallback` 增加 memory 分支（正则抽 subject）；
- `answer_validator.REQUIRED_SECTION_KIND` 增加 `"memory_query": "memory"`，composer 增加 memory 分区说明，`fallback_composer` 增加 `_memory_section`。

排除项：`preference`（写操作）、`event_collection`（写 + 交互式暂停）、`chitchat`（非 scoped query，单意图已有快路径）、`mcp_tool`（高险、默认禁用）。

### 阶段 1：放开依赖

允许 `depends_on`，但要求有向无环且依赖仅在已授权意图内。`batches()` 已按 priority + depends_on 计算批次，执行层零改动。

### 阶段 2：行程规划 / 合规入场（多意图真正的主场）

- 第 2 层：`compile()` 不再要求 `len(rules) == 1`，把 `itinerary_planning` 展开成完整执行链（`event_collection → rag_knowledge / information_query → itinerary_planning → trip_compliance`，对应 `hommey.yaml` 的 execution 步骤），按步骤接 `depends_on`。
- 关键设计：**意图节点级去重**。行程链内需要 `rag_knowledge`，用户又直接问"补贴"时，两个意图在 DAG 层合并为同一个 policy 节点、一个 scoped query。这是安全的去重——共享的是同一"查制度"意图，绝不混入天气等其他意图的内容。
- 配套：`trip` 分区的 Composer 渲染与校验、跨轮次"收集→暂停→续跑"（v1 已有 `_pause_incomplete_trip_planning` / `_continue_ready_trip_planning`，二期需在任务图语义下重新表达）。

### 阶段 3：写操作意图

`preference` / `event_collection` 允许 `side_effect: true`：写操作失败不影响并行读任务；答案卡中以 notice 呈现，不占 policy/weather 类正分区。

### 横切：scope 校验去"配对"化

第 4 层目前按意图对写死，意图一多就是 N² 条规则。可扩展做法：在 `hommey.yaml` 给每个意图声明 subject domain（如 `scope_keywords` 字段），用全局"词 → 域"词典，让 validator 泛化检查"task.query 的实体词 ⊆ 其意图的域"。新增意图改 yaml 即可进入校验，符合项目 skill 平台"单一事实来源"的哲学。

### 每阶段底线

- 入口放宽与 scope 规则、fallback、composer 分区**同步落地**；
- `AnswerSection.kind` 新增值（memory / trip 等）后，**前端卡片渲染映射需同步**（`webui_new` 目前大概率只认 policy/weather）；
- 依赖、并行、局部失败、grounded composition 各补测试（参考 `tests/test_task_orchestration_v2.py` 的断言方式：`"天气" not in seen["rag_knowledge"]`）。

## 完整性语义

- 并行任务各自失败互不影响；失败分区保留在答案卡中并标 error。
- Composer 只能整理任务结果，不拥有来源字段；校验分区覆盖与数字事实，违反则回退确定性 Composer。
- 文字版与结构化 `AnswerDocument` 同时生成，兼容历史客户端与导出。
