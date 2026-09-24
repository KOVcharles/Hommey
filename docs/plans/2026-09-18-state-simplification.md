# 主 Agent 状态与上下文机制：架构级重构

> **2026-09-19 实施说明**：已完成代码复核并实施状态/上下文简化。本文保留提案原貌；其中“表单待答分支是死代码”、`trip_version` 类型、附件输入完整性和跨轮复制执行账本等结论已被修正。实际改动、迁移边界与验证见 [实施记录](2026-09-19-state-simplification-implementation.md)。

**日期**：2026-09-18（初稿）／2026-09-19（二次评审后修订）
**分支**：`codex/pending-input-binding`
**范围**：Phase 1（现状分析）+ Phase 2（目标模型）。**不含任何代码改动。**
**判据**：删除了多少不必要的状态与同步逻辑，同时系统仍然更可靠。

> **修订记录**
>
> 改动处均以 **【修订】** 标注。**注意第 4 行与第 10 行**：同一个数字我算错了两次，第三次才收敛。这不是细节——它说明"运行时到底替模型做了多少决定"这个问题，靠印象回答必错。
>
> | # | 出处 | 原结论 | 修订后 | 原因 |
> |---|---|---|---|---|
> | 1 | §2.6 初稿 | 切断渲染回流，assistant 散文不进 `recent` | **推翻**，保留散文 | 会让问句失去唯一载体；§1.12 证明运行时没有别的通道 |
> | 2 | §2.2 初稿 | 新增 `goal`、`open_question` 持久字段 | **删除两者** | 散文已承载；`goal` 语义未决，改名后仍冗余 |
> | 3 | §2.12 初稿 | 不变式 `waiting_input ⟺ open_question ≠ None` | **整节删除** | 没有字段就没有不变式；连带消掉三处"修正"中的一处 |
> | 4 | §1.10 初稿 | `planning_ready` 有 **5** 处用途 | 二次评审改为 **9** 处 | grep 复核 |
> | 5 | — | — | **新增 §1.12** | 卡片能力的唯一生产者在运行时，删 gate 不等于交出能力 |
> | 6 | — | — | **新增 §2.6** `request_trip_details` | 把"何时弹卡片"的决定权挪给模型，而非删掉该能力 |
> | 7 | §2.3 二次评审 | payload 含 `missing` | **删除** | 全量注入 `facts` 后模型可自行判断；`missing` 是运行时消化过的结论，不是事实 |
> | 8 | §2.3 二次评审 | payload 含 `current_request` | **删除** | 与 `conversation[-1]` 重复；改由装配保证二者恒等（§2.3 不变式 1） |
> | 9 | §2.5 二次评审 | `evaluate_trip_intake` 降为"纯报告函数"，消费者是 payload | **修正消费者**：唯一消费者是 `build_trip_intake_document` | `trip_intake_document.py:131,160-167,215` 实际在用；payload 不再是消费者 |
> | 10 | §1.10 二次评审 | `planning_ready` 有 **9** 处用途 | **三次评审改为 11 处**（又漏 `:327`/`:677`） | grep 复核 |
> | 11 | §2.8 二次评审 | `work` 每轮重置，仅 `is_continue` 恢复 | **改为无条件跨轮继承 + `stale`** | 与 `is_continue` 门一起删除；见 §2.8 |

---

## 0. 结论先行

这次重构的对象不是「`"北京"` 没被识别」这一处缺陷，而是**这套状态设计本身在生产中从未被激活**这一事实。三条实测结论把问题从"哪里漏了同步"重新定义为"哪些东西根本不该存在"：

| 实测（71 次 `supervisor_runs`） | 数值 |
|---|---|
| `checkpoint.resolved_input` 非空 | **0 次** |
| `checkpoint.reply_to` 非空 | **0 次** |
| `checkpoint.pending_input` 非空 | **1 次**（且来自模型手填，非运行时合成） |
| `checkpoint.choice_output` 非空 | 3 次 |

`PendingInput`、`resolve_reply`、`previous_question`、`resolved_input`、`reply_to` 这一整套「待答绑定」机制，在 71 次生产运行里**产出过 1 次状态、0 次绑定**。它不是失效了，是**从未生效**。

因此 Phase 2 的形态是**削减**而非补齐：删字段、删同步点、把绑定的推理权交回模型。

**【修订】目标形态比初稿更简单。** 二次评审确认：模型上一轮问了什么、说了什么，**都已经在对话散文里**（§1.12），不需要任何新字段去重建它。状态里唯一必须保留的，是那些**无法从别处重建的事实**——也就是业务字段和执行账本。初稿曾试图用一条不变式来约束"等待输入"这个状态，修订后连这个状态本身都不再需要结构化表达。

---

## 1. Phase 1 — 当前状态分析

### 1.1 持久状态全集

持久层只有一处：PostgreSQL `supervisor_runs`，主键 `(user_id, request_id)`，列 `checkpoint JSONB / response / mutations / cancel_requested / owner / retention_until`。`checkpoint` 是 `agent_runtime/engine.py:112` 里 `deepcopy(self.state)` 的整块快照。

实测 20 个顶层键的出现率与读写责任：

| key | 出现率 | 写入者 | 读取者 | 生命周期 |
|---|---|---|---|---|
| `trip` | 100% | `engine.py:203` 初始化；`:950` apply_changes 回执 | 注入 `messages[1]`、`evaluate_trip_intake`、`validation`、`deliverable_results`、子 context、`prepare_trip_options` 门控 | 跨轮（源自 `active_trip_contexts`） |
| `version` | 100% | `:203` `trip_version(trip)` | 结果过期判定、`previous_question`、`task_key`、`restore_results` | 跨轮 |
| `results` | 100% | delegate 写入 `:1114` | finish / apply / discard / restore | 本轮 |
| `applied_results` | 100% | `:956` | `deliverable_results:165`、`can_show_intake:79`、`restore_results`、`_delegate:1017` | 本轮 |
| `discarded_results` | 100% | `discard_result` | 同上 | 本轮 |
| `preferences_updated` | 100% | `:953` | 输出回传 | 本轮 |
| `children` | 100% | `:1081` | 子任务复用、`source_bytes` 统计 | 本轮 |
| `calls` | 100% | `:1069` | `max_children` 上限 | 本轮 |
| `work_items` | 90.1% | `:1082` / restore | `execution_plan`、`_delegate:1047` 复用判定 | 本轮 |
| `work_context` | 100% | `:205` 初始化；`engine.py:1127-1129` 每轮写 `{task[:500], summary[:1000], missing_info, version}`；`:952` 在 new/cancel 时清空 | 注入 `messages[1]`；子 context | 跨轮 |
| `main` | 100% | `:208` | 主 agent 循环 | 本轮（每轮重建） |
| `checkpoint_version` | 90.1% | `:204` | `restore_results:68` | 跨轮 |
| `public_plan` | 90.1% | `:276` | UI 事件 | 派生（`execution_plan.snapshot`） |
| `control` | 90.1% | `:278` | `previous_question:27`；UI | 跨轮 |
| `home_location` | 35.2% | `:223` | 注入 | 派生（每轮重读） |
| `intake_submission_applied` | 31.0% | 表单提交路径 | `_delegate:1012` | 本轮 |
| `resolved_input` | 19.7% 键存在 / **0 非空** | `:194`、`:206` | 注入、子 context、`validation.py:77-79` | 本轮 |
| `reply_to` | 19.7% 键存在 / **0 非空** | `:206` | `:235` 澄清分支、子 context:1075 | 本轮（写一次，永不更新） |
| `pending_input` | 18.3% 键存在 / **1 非空** | `record_question:71` | `previous_question:29` | 跨轮 |
| `choice_output` | 4.2% 键存在 / 3 非空 | `:397` | `fallback():448` | 本轮 |

同一份业务事实（行程字段、上一轮结论）至少存在于 **7 个位置**：`state["trip"]`、注入的 `context.trip`、`context.previous_work`、`context.work_context`、`state["work_items"]`、`state["results"]` 的 tool message、以及跨轮落盘的 `active_trip_contexts`。用户列为问题的第一条，在数据上成立。

### 1.2 一轮 Turn 的实际数据流

```
webui_new/manager.py:474-517
  ├─ 用户原文 → add_message("user", normalized.display_message)  → conversation_messages
  │                                      ↑ manager.py:484，在 :493 run() 之前
  └─ Supervisor.run(scope, agent_text, user_text, trip_input)
       │
       engine.py:183  store.call("begin", fingerprint(text, user_text))   ← 幂等
       │   record.response 非空 → 直接回放，跳过一切重算
       │
       engine.py:187  self.state = record.checkpoint  (重试时是冻结的旧状态)
       │
       ├─ 新状态（:190-219）
       │    services.context(scope)  → {recent[8], session_summaries[2], trip, today}
       │    store.call("previous")   → 同 session 上一条 supervisor_runs
       │    pending  = previous_question(previous, trip_version(trip))     :192
       │    resolved = resolve_reply(user_text, pending)                   :193
       │    context["pending_input"|"resolved_input"] = pending|resolved   :194
       │    context["previous_work"|"work_context"] = 上一轮投影            :197-202
       │    state = {…20 键…}                                              :203-209
       │    main.messages = [system MAIN_RULES,
       │                     user json({context, current_request})]        :208-209
       │
       ├─ 守卫（:227-244）
       │    allow_short = resolved or trip_input or text≠user_text
       │                or is_intake_entry or (is_continue and work_context)
       │    guard_user_input(user_text, allow_short_reply=allow_short)
       │      unsupported → refuse / unclear → clarification / chitchat → 固定话术
       │
       ├─ 否则 execute_work()：主 agent 循环（delegate / read_result / apply_changes / finish）
       │
       ├─ :270-278  追加 agents / preferences_updated / engine
       │            execution_plan.settle(outcome)
       │            record_question(self.state, output)      ← pending 的唯一写入点
       │            control.update(status, outcome, stop_reason)
       │
       └─ :279 save(output, "completed")  → checkpoint = deepcopy(state)

manager.py:502-515
  └─ add_message("assistant", output["response"], {answer_document, presentation_document})
       output["response"] == document.plain_text  → 落回 conversation_messages.content
       → 下一轮 services.context() 的 recent[i].content
```

**闭环**：渲染出的自然语言经 `plain_text → response → conversation_messages.content → recent` 重新进入模型上下文。用户列出的第 4 条问题（"渲染后的自然语言重新进入 recent"）在代码上成立。注意 `answer_document` / `presentation_document` 两个结构化列**从不进入模型**（`services.py:95-97` 只 SELECT `role, content, sequence_no`），进去的只有被压平成散文的那一份。

**【修订】这个"闭环"是本次设计的支点。** 初稿把它当成缺陷（"渲染产物回流"），修订后确认它是**唯一的问答交接通道**（§1.12）：模型上一轮问了什么，只在这里。因此目标形态不是切断它，而是**保证它完整**（§2.4）。

### 1.3 pending 调用链（决定性的死亡结论）

```
写：engine.py:275 record_question(state, output)
      output.pop("_pending_input")                     dialogue.py:63
      ├ outcome != waiting_input  → state["pending_input"] = None        :65
      └ outcome == waiting_input
          document = output.get("presentation_document") or {}           :67
          document.get("type") == "trip_intake" and missing_required     :68  ← 死分支
          state["pending_input"] = {**pending, trip_version} if pending else None  :71

读：engine.py:192 previous_question(previous, version)  dialogue.py:23-36
      要求 previous.status == "completed"
      且 previous.checkpoint.control.outcome == "waiting_input"
      且 checkpoint.pending_input 是 dict 且 trip_version 匹配且 question 非空
      → resolve_reply(user_text, pending)               dialogue.py:39-59
```

**`dialogue.py:68-70` 的 `trip_intake` 合成分支是死代码。** `output` 由 `render()` 产出，而 `render.py:14` 与 `render.py:82` 两条返回路径都硬编码 `"presentation_document": None`。`agent_runtime` 内唯一产出非空 `presentation_document` 的是 `engine.py:58-61` 的 `intake_output()`，它走的是 `engine.py:176` / `:243` / `:980` 三条快速路径，而这三条的全部分支在 `:275` 都会因 `output.get("presentation_document")` 为 `None` 而落到 `:71` 的 else。实测吻合：8 次 `trip_intake` 终态运行，`pending_input` 非空 **0 次**。

所以 `pending_input` 的唯一实际来源是模型的 `finish(pending_input=…)`（`engine.py:998-999`）。71 次里模型填了 **1 次**。

于是缺失链条是确定的：

```
模型发 finish(kind="ask", question="请告知您从哪个城市出发前往南京…")，不带 pending_input
  → :998 不成立 → 没有 _pending_input
  → dialogue.py:71 → state["pending_input"] = None
  → :278 control.outcome = "waiting_input"
  → 落盘后即成为「waiting_input 但 pending_input=null」的不变式违反
下一轮
  → previous_question 在 :29 读到 None → 返回 None
  → resolve_reply(text, None) 立即 return None
  → :227 allow_short 的五项全假
  → guard_user_input("北京", allow_short_reply=False)
  → core/intent_guard.py:65 判为 unclear
  → 用户看到一句澄清，而非被补全出发地
```

自引用陷阱：`allow_short` 依赖的 `resolved_input`，正是被同一处缺失算没的。**这是"北京"bug 的完整根因，不是缺陷列表里的一个 —— 它是唯一的一条。**

### 1.4 pending 状态的实测形态

修复提交 `270810f`（2026-09-17）之后的 14 次运行：

| 指标 | 数值 |
|---|---|
| 运行数 | 14 |
| `control.outcome == "waiting_input"` | 9 |
| 其中 `pending_input = null` | **8**（89% 违反不变式） |
| 其中 `pending_input` 有值 | 1（`f8a77bdaf0d3`，`field=destination`，来自模型手填） |
| `resolved_input` 非空 | 0 |
| `reply_to` 非空 | 0 |

另有 `cdd74610-…`：`pending_input` 键**完全缺失**（不是 null）。该轮走 `engine.py:287` 的 `RuntimeStopped` 分支 → `store.call("stop", …, deepcopy(self.state))`，**绕过了 `:275 record_question`**。即：中断路径与正常路径对同一份状态写了两种不同形状。

南京现场（session `205dec9f-…`，2026-09-18 07:57–07:59）的 `35d1f2be` 终态：

```
control           = {status: completed, outcome: waiting_input, stop_reason: null}
pending_input     = null
resolved_input    = null
reply_to          = null
main.terminal     = {outcome: waiting_input,
                     presentation_document: null,          ← 无待答问题的结构
                     answer_document.sections[0].body: "请告知您从哪个城市出发前往南京，以便查询车次信息。"}
                     ← 问题只以渲染后的散文存在
```

**契约缺口**：「运行时展示了一个自由文本问题」这一状态**没有任何数据结构可以表达**。`PendingInput`（`contracts.py:28-43`）的校验器 `one_input_type` 要求 `field` 与 `choices` 恰有其一，两者都是**封闭枚举**（9 个行程字段 / 编号选项）。一个不落在枚举里的自由文本问题——恰恰是最常见的情形——无处安放。

**【修订】这条"契约缺口"的正确解法不是补结构，是承认散文就是契约。** 问句已经以 `answer_document.sections[0].body` 的形式存下来了，且**与用户看到的完全一致**。任何结构化重建（枚举、字段、版本戳）都是对同一事实的二次编码——而这正是 §1.11 那 13 处分歧的生成机制。

### 1.5 三种"终态"互相漂移

| 位置 | `waiting_input` 计数 | 说明 |
|---|---|---|
| `checkpoint.control.outcome` | 30 | 全量 71 次 |
| `checkpoint.main.terminal.outcome` | 11 | 键存在 11 次（旧结构还在） |
| `checkpoint.main.terminal.presentation_document` | 8 | 全部集中在 09-11…09-15 |

同一语义在三个地方各存一份，且计数互相不一致。

### 1.6 `reply_to` 是一个只写不读的死字段

`engine.py:206` 把上一轮的 `pending` 快照写进 `state["reply_to"]`，此后**再无任何写入**。读取点只有两处：

- `engine.py:235-237`：`unclear` 且 `user_text.isdecimal()` 时重发 `reply_to` 的问题。但走到这一分支的前提是 `allow_short` 为假，而 `allow_short` 为真时才会设置 `resolved_input`；`reply_to = pending`，`pending` 非空则 `resolved` 通常非空 → 该分支几乎不可达。
- `engine.py:1075`：子 agent context 里 `"pending_input": self.state.get("reply_to")`。

第二处是**命名冲突**：主 agent 看到的 `context.pending_input` 是本轮新算的 `pending`，子 agent 看到的 `pending_input` 是**上一轮的陈旧快照**，同名不同义。实测 `reply_to` 在 71 次里非空 0 次，两处读取都从未取得过非空值。

### 1.7 `choice_output`：渲染产物被写进状态

`engine.py:397`：

```python
self.state["choice_output"] = {"version": self.state["version"], "output": deepcopy(output)}
```

`output` 是**完整渲染后的输出**，含 `answer_document` 与 `trip_options`。这是"渲染内容进入状态"的教科书式反模式。实测触发 3 次。

### 1.8 上下文注入的实际形态

`messages[1]` = `json.dumps({"context": context, "current_request": self.text})`，`context` 的构成：

| key | 来源 | 截断 |
|---|---|---|
| `recent` | `conversation_messages` 最近 8 条（`services.py:95-98`） | 每条 `content[:1400]`，**无总预算** |
| `session_summaries` | `session_summaries` 表最近 2 条（`:99-102`） | 每条 `[:1600]` |
| `trip` | `active_trip_contexts.context_data` | 无 |
| `today` | `beijing_today()` | 无 |
| `pending_input` | `previous_question(...)` | 无 |
| `resolved_input` | `resolve_reply(...)` | 无 |
| `previous_work` | 上一轮 checkpoint 的 `results` 末 6 条投影 | `summary[:500]` |
| `work_context` | 上一轮 checkpoint 的 `work_context` | 无 |

三个额外事实：

1. **`session_summaries` 在生产中恒为 `[]`。** `claim_summary_range` / `insert_session_summary`（`memory_repository.py:608-752`）在仓库内**没有任何生产调用方**（仅测试与文档）；`memory_manager.py:178` 只留了一行节注释。注入的这一路是死的。
2. **`trip` 未做业务字段过滤。** `store._apply` 落盘 `{**current, **trip, "status": "active", "_trip_id": …}`（`store.py:170-179`）；`engine.py:927, 934-935, 943` 还会加入 `_capability_selection`、`work_location_verified`（完整地点字典）、`work_location`。这些内部标记因此原样进入模型上下文。
3. **重试时上下文是冻结的。** `engine.py:187` 直接取回 checkpoint，`messages` 不重建。同一 `request_id` 的第二次调用看到的是上一次的 JSON。

子 agent context（`engine.py:1072-1080`）与注释「only current user text, a small trip snapshot, and explicitly selected dependency results」不符——实际还包含 `resolved_input`、`pending_input`（= `reply_to`）、`today`、完整 `dependencies`。真正缺席的是父 transcript、父 tool log、`state["results"]` 全体。

### 1.9 边界层重复

- `webui_new/manager.py:202-234`：当 `presentation_document` 列为 null 时，**从 assistant 的 `content` 纯文本反推**重建整份 `presentation_document`（`recover_trip_intake_document`）并回写数据库。这是 webui 拥有的一份业务文档再推导，可能与 supervisor 原始产出不一致。
- `webui_new/static/trip-intake-card.js:831-852`：客户端把卡片内嵌的 `trip_input` 快照与本地输入合并、并**自行重算** `end_date = start_date + duration_days - 1`，再作为 `trip_input` 提交。客户端持有可与服务端分歧的行程副本（服务端靠 `validation.py` 兜底拒绝）。
- `manager.py:169-175` 的 `archived` 由持久行顺序推导；`manager.py:184-200` 的卡片过期由 webui 自行读历史判定。

### 1.10 运行时承担的业务判断（实际规模）

运行时不只是"做了几个判断"——它**实现了一整条业务流程**，并且这条流程**绕过模型**。

**`evaluate_trip_intake`（`core/trip_intake.py:128-208`）是一个工作流驱动器，不是校验器。**

**【修订】`planning_ready` 的实际调用点，经三轮 grep 复核，最终为 11 处**（初稿称 5 处，二次评审改为 9 处，均漏）：

| # | 位置 | 决定 | 轮次 |
|---|---|---|---|
| 1 | `engine.py:146` | `read_skill` 是否出现在工具表里（且 `collecting` 还叠加 `can_show_intake`） | 初稿 |
| 2 | `engine.py:175` | `waiting_for_input()` 的终态是卡片还是 ask | 初稿 |
| 3 | `engine.py:242` | 裸"出差"直接出表单，**完全不调模型** | 初稿 |
| 4 | `engine.py:327` | **是否尝试 `parse_trip_entry` 解析用户原文** | **三次** |
| 5 | `engine.py:354` | 表单提交后是否直接返回卡片 | 二次 |
| 6 | `engine.py:368` | `prepare_options` 内部的第二道阻断 | 二次 |
| 7 | `engine.py:444` | `fallback()` 超时/降级路径的同款判断 | 二次 |
| 8 | `engine.py:677` | **`prepare_options` 的调用方入口**（`loop` 内的自动触发） | **三次** |
| 9 | `engine.py:827` | 强制把 `trip_context` 的 `status` 改成 `needs_input`，覆写 `missing_info` | 初稿 |
| 10 | `engine.py:977` | 终态是行程卡片、ask 还是 clarify；**覆盖模型自己的 finish** | 初稿 |
| 11 | `engine.py:1021` | 是否允许委派 `trip_planner` | 初稿 |

第 4 处（`:327`）最值得注意：它决定了**用户原话要不要被当成行程字段去解析**。这不是"缺料时弹个卡"，是"缺料时连试都不试"。`prepare_options` 相关的两处（`:368` 内部 + `:677` 入口）同理——运行时不仅决定了缺料时的展示，还决定了缺料时**根本不进入规划流程**。

另有 `engine.py:375` 的 `validated_trip_anchor` 门（独立的锚点检查，不是 `planning_ready`，但同属运行时业务判断）。`evaluate_trip_intake` 里真正属于"校验"的只有 `invalid_fields` 与 `conflicts`。

**硬编码的业务流水线**（`engine.py:403-440` `complete_trip`）：顺序固定为
`policy_rag → trip_planner → [compliance]`，
每步的 `task` 文本由运行时用中文写死（`:410-414`，例如"核实本次完整企业差旅的目的地城市等级、各职级住宿限额、餐补…"），合规步是否追加取决于 `user_text` 里有没有 `合规`/`检查`（`:413-414`），策略失败时的补充措辞也是运行时拼的（`:431-432`）。运行时代模型读了自己的 skill 文件（`:406-407` 读 `plan-trip:references/complete-trip.md`）。

**`prepare_options`（`engine.py:360-401`）**：自动把 `weather` 与 `local_transport` 加入查询集（`:380-382`）；无锚点时直接弹地点选择表单阻断交付（`:374-375`）；把 `planning_facts` 伪造成一条 `travel_info` 的 `Report` 塞进结果集（`:383-395`）——运行时代模型"提出证据"。

**`full_trip.complete_output`（`:19-38`）** 写死交付契约：`required = {"policy_rag", "trip_planner"}`；缺项时由运行时拼中文告知；任何非 success 一律降级为 `partial`。

**`trip_options.options_output`（`:155-168`）** 由运行时计算业务终态 `outcome`（`needs_input` / `partial` / `completed`）。

**其余运行时业务判断**：`main_tool_names`（`:142-154`，按"是否已受理"、"行程是否完整"动态裁剪工具集）、`deliverable_results`（`:156-170`，提案未提交即不可交付）、`can_show_intake`（`dialogue.py:74-80`）、`fast_routes.weather_policy_tasks`（`:5-23`，整句正则，命中则完全跳过 supervisor 循环）、`is_direct_policy_query`（`:64-69`）、`parse_intake_submission` / `parse_trip_entry`（`intake_submission.py`，运行时解析中文表单）、`grounded_date`（`validation.py:16-34`，运行时拥有中文相对日期语义）、`trip_options.exclusions`（`:14-19`，运行时正则识别否定）、`rank_trains`（`:22-31`，运行时做车次排序）、依赖自动补线（`:1025-1043`）、`invoke_specialist` 的 8 段结果改写（`:790-858`）。

### 1.11 同一业务事实的多副本：13 处可证分歧

用户第 1 条问题（同一状态在多个字段重复）不只是"重复"，是**重复且互相矛盾**。逐条可证：

| # | 分歧对 | 机制 |
|---|---|---|
| 1 | `state.version` vs `results[id].input_version` | `apply_changes` 只更新被应用结果（`engine.py:954`），其余保持旧值；`results()` 随后拒绝它们（`:741-742`） |
| 2 | `applied_results` vs DB `mutations` | `:944-946` 在**无任何 DB 写**的路径上追加 `applied_results`；`:213` 又会在结果未恢复时丢弃该 id，而 DB 回执与已提交行程仍在 |
| 3 | `checkpoint.trip` vs `active_trip_contexts` (cancel) | `store.py:188-189` 在 upsert **之后**把 `current = {}`，于是回执与 `state["trip"]` 为空，而 DB 行仍是完整已取消行程 |
| 4 | `work_items[k].status` vs `results[id].status` | `settle` 只写 work_items（`execution_plan.py:78-83`），`deliverable_results`/`fallback` 仍读到 `success`；`snapshot` 又映射成第三种词汇 `succeeded` |
| 5 | `control` vs DB `status` vs `public_plan.status` | `RuntimeStopped` 路径从不更新 `control`（停在 `running`），DB 变 `interrupted`，plan 说 `cancelled`；`store._public_plans:137-142` 在读取时打补丁 |
| 6 | `reply_to` vs `pending_input` | 同名同形（都是 `{question, input, trip_version}`），前者是**上一轮**问题，后者是**本轮**问题；一轮内既回答又提问时二者不一致 |
| 7 | `main.messages[1]` vs 实时 `state.trip` | `:208-209` 把开轮上下文**冻结**进 messages[1]；`:950` 之后改动 `state["trip"]` 不动副本；`capture_evaluation`(:524) 读的是旧副本 |
| 8 | `state.results` vs `main.messages` 里的 tool message | `delegate` 返回值被原样追加为 tool message（`:640`）；`compact_tool_history` 只裁剪 transcript 一侧，`read_result` 读 `results` 一侧 |
| 9 | `intake_submission_applied` vs `work_items["intake_submission"]` vs `results["result_intake_submission"]` | 三者描述同一件事；只有第一个每轮重置 |
| 10 | `preferences_updated` vs DB 偏好行 | `:953` 从回执 OR 进来，`:207` 每轮重置为 `False` 且从不恢复；DB 写已提交而该轮上报 `False` |
| 11 | `choice_output.version` vs `state.version` | `:397` 打戳，`:449` 要求相等才复用——代码自己承认二者会漂移 |
| 12 | `children[k].version` vs `state.version` vs `work_items[k].input_version` | 子循环建时打戳（`:1078`）、结果由 `child["version"]` 构造（`:1112`）、账本事后才对账（`:1124-1126`）——账本在委派期间滞后 |
| 13 | `calls` vs 进程级预算 | `:1068` 先扣全局预算再写 `children`（`:1081`）；`calls` 每轮重置为 0 而预算不重置 |

外加一条**只写不读的死字段**：`children[k].stop_reason`（`:1105, :1110` 写，`agent_runtime/` 内无任何读取方）。

**缺失的不只是 pending 这一条不变式，是这 13 对字段之间一条都没有。** 状态被当成"随手可放东西的字典"，而不是一个有约束的类型。

### 1.12 【修订】问答交接的唯一通道：assistant 散文

初稿把"渲染产物回流 `recent`"列为缺陷（§2.6 试图切断）。二次评审确认**这个结论是错的**，理由是运行时没有第二条通道。

**问句在所有产出路径上都进入 `response`：**

| 产出路径 | 问句落到哪 |
|---|---|
| `render()` 的 `ask` / `clarify` 分支 | `render.py:78-79` 把 `finish.question` 原样放进 notice section 的 `body` |
| `intake_output()`（行程卡片） | `engine.py:61` `response = document["plain_text"]` |
| `degraded_output()`（降级通知） | `control.py:54` 走 `render(Finish(kind="ask", question=notice))` |

而 `response` 正是 `manager.py:514` 写进 `conversation_messages` 的那一列，也是下一轮 `services.context()` 读回的 `recent`。

同时——**卡片能力的唯一生产者是运行时**：

```
presentation_document 的全部产出点：
  render.py:82                → 硬编码 None
  trip_options.py:166         → 硬编码 None
  engine.py:61 intake_output  → 唯一产出非 None 的地方

intake_output 的调用点：engine.py:139 ← 由 :243 / :355 / :369 / :375 / :445 / :980 触发
                        全部被 planning_ready / can_show_intake 控制

MAIN_TOOLS（engine.py:39-47）7 个工具里，没有任何一个能产出 presentation_document
```

**两条结论：**

1. 问句必须留在散文里。切断 assistant 散文（初稿 §2.6）会让模型再也看不到自己上一轮问了什么——`"北京"` bug 会以更彻底的形式复现。
2. 删除 gate **不等于**把卡片能力交给模型。模型侧根本无从产出 `presentation_document`。要让"模型决定何时弹卡片"成立，必须先给模型一个工具（§2.6）。

**散文作为交接载体的唯一风险是截断**，见 §2.4。

---

## 2. Phase 2 — 目标模型设计

### 2.1 设计公理

1. **状态只存"不可从别处重建的事实"。** 能从 DB、对话散文或 `results` 重算的，一律不存。
2. **渲染产物永不进入状态。** 状态里只放结构化事实，散文只出现在出口。
3. **绑定的推理权归模型。** 运行时不判断"这句话是不是在回答上一问"；运行时只保证**上一问被完整、无歧义地交给了模型**。
4. **信息给足，不做裁决。** 运行时要么给信息，要么什么都不做；不以"替模型判断"的形式介入。（初稿的"一条不变式"公理已删除，见 §2.12）
5. **中断路径与正常路径写同一种形状。**
6. **名字必须等于机制。** 不允许出现语义靠猜、或与机制不符的字段名（§2.2 的 `goal` 即因此被删）。
7. **读取永不拒绝；拒绝只发生在写入与交付。**

   公理 7 是二次评审的产物，单独解释：**读取是模型了解世界的方式，拒绝读取等于让它在黑暗中做决定。** `read_result` / `read_source` 只要 id 存在且未被丢弃就应当成功——包括结果已过期、行程已变更的情形。**过期是内容的一部分，不是禁止读取的理由**：模型读到"这条车次是旧版本行程的"，才能自己判断要不要重查。

   把 `input_version` 与证据时效两道门从读取侧移到**写入与交付侧**（`apply_changes` / `finish` / `deliverable_results`），一次解决三个问题：

   - 跨轮继承的 `work` 里那些过期条目还能读（§2.8 的 15 分钟窗口问题）
   - `stale` 标记从"读取的前置条件"变成"读取的返回内容"，模型不需要预判
   - 拒绝只发生在"要动数据"或"要交付给用户"的时刻——那才是真正需要保证正确性的地方

   代价：`finish(answer, [过期结果])` 仍会被拒。但这是**可预判**的——`work[].stale` 在 payload 里，`read_result` 的返回里也带同样的位。运行时不再有模型看不见的判据。

### 2.2 目标 `AgentState`

**【修订】初稿在此新增 `goal` 与 `OpenQuestion` 两个持久字段，均删除。**

```python
class AgentState(StrictModel):
    # —— 跨轮持久：业务事实（唯一真源在 active_trip_contexts，这里是投影后的快照）
    facts: TripFields              # 由 trip 改名：角色中立
    version: int
    # —— 本轮产物 ——
    results: dict[str, SpecialistResult]      # 保留
    work_items: dict[str, WorkItem]           # 保留（执行安全）
    children: dict[str, Child]                # 保留（执行安全 + 断点恢复的记录载体）
    calls: int                                # 保留（执行安全）
    # —— 运行时控制 ——
    control: Control                          # status/outcome/stop_reason/revision
    main: MainLoop                            # messages/round
    home_location: str = ""
    applied_results: set[str] = set()
    discarded_results: set[str] = set()
    preferences_updated: bool = False
    public_plan: dict = {}                    # 降为纯投影（revision 已移入 control）
```

**删除 6 个字段，新增 0 个，改名 1 个，降级 1 个。**

| 旧字段 | 结论 | 理由 |
|---|---|---|
| `pending_input` | **删除** | 封闭枚举装不下自由文本问题；实测 71 次仅 1 次非空；散文已承载同一事实（§1.12） |
| `resolved_input` | **删除** | 实测 0 次非空；值是 `f(user_text, pending)` 的纯函数 |
| `reply_to` | **删除** | 实测 0 次非空；写一次永不更新；在子 context 里还造成同名不同义 |
| `choice_output` | **删除** | 渲染产物入状态；`fallback()` 应改为**重新渲染**而非回放旧渲染 |
| `work_context` | **删除** | 与 `previous_work`、`results` 三重重复；实测只被注入、从不被判定 |
| `checkpoint_version` | **删除** | 只有 `restore_results:68` 一个消费者；迁移期一次性清空历史 checkpoint 即可 |
| `intake_submission_applied` | **降级为日志** | 单个字符串判据撑不起一个状态位 |
| `trip` | **改名为 `facts`** | 名字自带的"这是一个行程对象"暗示，正是 `_trip_id`/`work_location_verified`/`_capability_selection` 被塞进来的原因。改名是根治，白名单是防御 |
| ~~`goal`~~ | **【修订】不新增** | 定义为"本轮 `current_request` 原样透传"时，它恒等于 `conversation` 里的上一条 user 行（`manager.py:484` 在 `:493` 之前写库，中断轮也有），100% 可重建。定义为"跨轮累积会话目标"时，需要模型每轮重写、中断即陈旧、无人能判它是否还成立。两种定义下都不该新增字段 |
| ~~`OpenQuestion`~~ | **【修订】不新增** | 唯一作用是把散文里的问句再编码一遍结构化版本，正是 §1.11 的生成机制；且初稿自己写明读取侧"不做版本匹配"，`trip_version` 是只写不读的死字段——重蹈 `reply_to` 覆辙 |

净效果：状态 **20 键 → 14 键**（删 6、改名 1、降级 1）；跨轮持久字段 **6 → 3**（`facts / version / control`）。

`public_plan` **保留，但需拆出 `revision`**：`steps/depends_on/status/title/purpose/summary/时间戳` 全部可从 `work_items` 重算；但 `run_id` 与 `revision` **不可重算**——`revision` 是 `old["revision"] + 2` 的**自携带**值（`execution_plan.py:118-119`，`+2` 是为 `store._public_plans` 读取时投影留的间隙）。应把 `revision` 提为 `control` 上的单调计数器，`public_plan` 降为纯投影。

**删除的注入路**：`session_summaries`（生产恒为 `[]`：`claim_summary_range`/`insert_session_summary`（`memory_repository.py:608-752`）在仓库内无生产调用方，仅 `test_memory_summaries*.py` 覆盖，`memory_manager.py:178` 只留节注释）。

**依赖的既有机制**：`children` 承载子 agent 完整转录（`engine.py:1078-1081`），实测非空 38/71 次、最大 194,125 字符。它是断点恢复（§2.8）的唯一记录载体，必须保留。

### 2.3 目标注入 payload（`messages[1]`）

**这是每轮构造的注入上下文，不是持久状态。** 这个区分决定成败——一旦某个投影字段被写回 checkpoint，§1.11 的 13 处分歧会在新架构里原样重演。

```jsonc
{
  "facts": {                                   // ← DB active_trip_contexts 白名单投影
    "destination": "南京"
  },
  "work": [                                    // ← results ∪ work_items 的投影，只承担寻址
    { "result_id": "r1", "role": "travel_info",
      "status": "success", "stale": false },
    { "result_id": "r2", "role": "travel_info",
      "status": "needs_input", "stale": false }
  ],
  "conversation": [ ... ],                     // ← conversation_messages，预算化窗口（§2.4）
  "today": "2026-09-19"
}
```

**四个字段。没有第五个。** 先前设计过的 `missing` 与 `current_request` 均已删除，理由见下表。

**每个字段的来源必须声明：**

| 字段 | 来源 | 性质 |
|---|---|---|
| `facts` | DB `active_trip_contexts` → `TRIP_FIELDS` 白名单 | 投影 |
| `work` | `results` ∪ `work_items`，跨轮继承不带 `is_continue` 条件（§2.8） | 投影 |
| `conversation` | `conversation_messages`（预算化，§2.4），末条内容替换为本轮 `user_text` | 投影 |
| `today` | `beijing_today()` | 每轮 |

**被删掉的两个字段：**

| 曾设计 | 结论 | 理由 |
|---|---|---|
| `missing` | **删除** | `evaluate_trip_intake(facts)["missing_required"]` 是运行时消化过的**结论**，不是事实。`facts` 全量注入后模型手上就有判断所需的全部材料；再注入一份结论，等于让模型去核对一个它无法验证的二手判断——正是 §1.11 那 13 处分歧的生成机制。**"必填有哪几个"属于 skill 知识，写进 §2.6 的工具描述与 skill 引导。** |
| `current_request` | **删除** | 它是 `user_text` 的副本，而 `conversation` 末条已经是同一轮的 user 行（`manager.py:484` 在 `:493` 之前写库）。同一事实两个载体就是 §1.11。改由不变式 1 保证二者恒等 |

**七个必须钉死的点：**

1. **`work[].status` 必须用 `Report` 词表**（`success / partial / needs_input / unavailable / error`，`contracts.py:70`），不是 `WorkItem` 词表（`contracts.py:193`，含 `pending/running/skipped`）。因为 `read_result` 返回的是 `Report`——**索引词表与 `read_result` 必须一致**，否则模型要在两套词汇间自行对齐，就是 §1.11 的成因。现有第三套词汇 `public_plan` 的 `succeeded`（`execution_plan.py:114`）同样不得外泄。

2. **`work[]` 必须带 `stale`。** `engine.py:741-742`：`input_version != version` 直接 `ToolRejected("结果对应旧版行程")`。`apply_changes` 只更新被应用结果（`:954`），行程一改其余全部作废。**公理 7 把这道门留在交付侧**（`finish` / `apply_changes`），它仍会拒绝 `finish(answer, ["r1"])`——所以模型必须能事先看到哪些作废了，否则就是一道它无从预判的拒绝。`stale = r.input_version != state.version`，每轮纯函数重算，零新增状态。

3. **`work[]` 每项必须有 `result_id`。** `read_result` 按 `result_id` 索引（`engine.py:41`, `:909-910`）；丢了它，"索引注入 + 按需读"就退化成"只有摘要可看"——而 `children` 实测最大 194,125 字符、模型上下文上限 80,000（`engine.py:566`），全量注入物理上不可能，按需读是唯一出路。

4. **`work[]` 不带 `summary`。** 内容已在 `conversation` 的散文里；`work` 只剩寻址职责。重复携带摘要会引入第二份可能与散文不一致的描述。

5. **`facts` 剥离内部标记。** 只注入 `TRIP_FIELDS`，去掉 `status` / `_trip_id` / `_capability_selection` / `work_location_verified`。子 agent context（`engine.py:1074`）同步收窄为**按角色的投影**——查天气的 `travel_info` 叶子拿到的应是 `{destination}`，不是整个行程对象。

6. **`conversation[-1].content` 必须等于本轮 `user_text`——由装配保证，不靠数据库。**

   现状有缺口：`manager.py:338` 里 `display_message = agent_query = text`，表单 JSON 直到 `:476-477` 才追加到 `user_text`/`agent_text`，而 `:484` 落库的是 `display_message`。**库里存的是用户看到的，不是 supervisor 消费到的。** 表单一提交，这个缺口就张开。

   修法不是加字段，也不是改库：**装配时把末条 user 行的内容替换为 `user_text`**。那一行本来就是本轮的消息，替换是精确的而非猜测；末条不是 user 行时直接追加一条。`text == user_text`（无表单、无附件的普通轮）时替换是恒等操作。UI 仍显示 `display_message`，模型拿到 `user_text`，两边各自正确。

   这条**一举消掉待确认项 2**（§3）。它也是"把整个对话的信息充分给到 agent"的兜底断言：`user_text` 一定在 payload 里，因为它是装配的最后一步。

7. **`work` 跨轮继承，且 `stale` 只在构造那一刻正确。**

   继承的理由：模型需要知道上一轮做过什么、什么已经交付不了。`engine.py:207` 每轮硬编码 `"children": {}`、`:203-207` 重置 `results`/`work_items`——**记录了，换轮就丢**，这才是"断点恢复"问题（§2.8）的真正形态，与 `is_continue` 无关。去掉 `is_continue` 门（`:210`）后继承成为默认行为。

   `stale` 的时效性是个陷阱：它按 `r.input_version != state.version` 在开轮算出，但 `apply_changes` 中途会把 `version` 推进，此后注入的那份 `stale` 就过期了。修法是**让写入方广播**——`apply_changes` 的回执除 `version` 外增加 `stale_results: [id]`，模型在需要的时刻拿到需要的信息，不必重新注入。

   `work` 是索引，需要有上限（建议 12 条，超出丢弃最旧）。**丢掉索引条目不影响可读性**：`read_result` 读的是 `state["results"]` 而不是 `work`，索引只承担"让模型知道有这回事"。上限是 discovery 的边界，不是数据的边界，必须在文档里写明而不是静默截断。

**削减掉的注入键**：`session_summaries`（死管道）、`resolved_input`（派生）、`work_context`（三重重复）、`previous_work`（并入 `work`）、`pending_input`（散文已承载）、`missing`（结论非事实）、`current_request`（与末条恒等）。

### 2.4 `conversation` 的预算化

**【修订】初稿 §2.6 主张切断 assistant 散文；现改为：散文保留，但保证完整。**

现状是 `services.py:95-98`：最近 8 条、每条 `content[:1400]`、**无总预算**。这个组合有一个可证的失效：

```
services.py:98     content[:1400]        头部截断
render.py:19       section.body = result.summary
contracts.py:71    Report.summary        max_length = 2400   ← 单个结果就能超 1400
render.py:78       问句 section 追加在最后                    ← 头部截断先吃掉它
render.py:81       plain_text[:12000]
```

**单个专业结果的 `summary` 上限 2400，已超过 `recent` 的 1400 截断线。** 一旦超过，被砍掉的正好是最后那条问句——用户看到了（UI 拿的是完整 12000），模型没看到。`"北京"` bug 换了个位置复现。

实测样本（南京 09-18 抓取，49 个 `content` 字段）最大 395 字符，离 1400 尚远，所以**当前未爆**——但那是短会话。完整出差交付（车次 + 天气 + 制度 + 规划）会爆。

**目标形态**：把 `recent` 换成有总预算的 `conversation` 窗口——

```python
conversation = build_window(
    rows,                      # 同 session 全部 conversation_messages
    budget=CONTEXT_BUDGET,     # 单一总预算，而非 8 × 1400
    recent_intact=1,           # 最近一轮完整保留，不参与截断
)
```

三条必须成立的性质：

1. **最近一轮完整。** 它是模型"我上一轮说了什么"的唯一来源（§1.12），不得被截断。
2. **更早的按预算压缩，而不是按条数硬截。** 8 条固定窗口在长会话里丢得太快，在短会话里又浪费预算。
3. **末条 user 行的内容是本轮 `user_text`，不是库里的 `display_message`**（§2.3 不变式 1）。装配的最后一步做这个替换——它同时回答了"附件和表单到底有没有给到 agent"这个问题。

这是 context assembly policy，不是 gate——它不改变任何语义，只决定喂给模型多少。

**唯一残留的未知**：修完预算后，问句能否稳定到达模型。这决定要不要退回一个 `text` 字段（§3.1）。**必须用真实的长交付场景验证，不能靠推演。**

### 2.5 运行时的职责分界（砍在哪）

| 保留在运行时（执行安全） | 交还模型（业务推理） |
|---|---|
| `fingerprint` 幂等、`begin/save/apply/stop` | **`planning_ready` 的全部 11 处用途**（`:146 :175 :242 :327 :354 :368 :444 :677 :827 :977 :1021`） |
| `task_key` 去重、`max_children`、`ROLE_TASK_LIMIT` | `can_show_intake`（`dialogue.py:74-80`，消费者全没了） |
| `UNCOMMITTED_TRIP`（写前必须先提交，防数据不一致） | `complete_trip` 的固定顺序与写死的 task 文本 |
| `source_bytes` / 80k context 上限 / `SOURCE_SIZE_LIMIT` | `prepare_options` 的自动加项与伪证据 |
| `validation.py` 全部写入门（原文依据、日期落地、天数范围） | `options_output` 的 outcome 计算 |
| `read_result` / `read_source` 的**存在性**检查（公理 7：只查存在，不查时效） | `read_result` 的**时效**判定（`input_version` / 15 分钟）——移到 `apply_changes`/`finish`/`deliverable_results` |
| `control.settle` 的终止语义 | 依赖自动补线（`:1025-1043`） |
| `request_trip_details`（新，§2.6）的渲染 | `unclear` 判定 |
| | `is_intake_entry` / `is_direct_policy_query` 的**未命中**语义 |

#### 十一处 gate 的逐个处置

| # | 位置 | 处置 | 理由 |
|---|---|---|---|
| 1 | `:977` `finish` 里覆盖模型结论 | **删** | 真正的越权：模型已 `finish`，运行时推翻它 |
| 2 | `:175` `waiting_for_input` 里同款覆盖 | **删** | 同上 |
| 3 | `:242` 裸"出差"跳过模型直接出卡片 | **改为纯命中优化** | 命中即调用 `request_trip_details`（§2.6），未命中不影响语义 |
| 4 | `:327` 是否尝试 `parse_trip_entry` | **换判据**，不是删 | 见下方专条 |
| 5 | `:354` 表单提交后直接返回卡片 | **改为命中优化** | 同 3 |
| 6 | `:368` `prepare_options` 内部第二道阻断 | **删** | 缺料时交给模型判断；卡片渲染由 `request_trip_details` 承担 |
| 7 | `:444` `fallback()` 同款判断 | **删** | 超时降级路径不应有独立的业务判断 |
| 8 | `:677` `prepare_options` 的调用方入口 | **删** | 与 6 是同一决定的两道门（入口 + 内部）。**保留两道中的一道没有意义**——要么都留要么都不留 |
| 9 | `:827` 强制改写 `trip_context` 的 `status`/`missing_info` | **删** | 让专家自己报的状态生效；`validation.py:71` 的原文依据门仍在兜底 |
| 10 | `:146` 收集中裁掉 `read_skill` | **删** | 代价是模型可能读多个 skill（`guidance[:12000]`，`engine.py:776`），由 `SUPERVISOR_CONTEXT_LIMIT` 兜底 |
| 11 | `:1021` 拦住 `trip_planner` 委派 | **删** | 缺料时 leaf 自己会返 `needs_input` + `missing_info`，那才是对的信号 |

`can_show_intake` 的全部 4 个调用点（`engine.py:146` / `:175` / `:444` / `:979`）**都在上表被删的位置里或紧邻其内**（`:979` 正是第 10 项 `:977` 块的下一行），因此它失去所有消费者，**连带删除**（`dialogue.py:74-80`）。`dialogue.py` 基本清空。

#### 专条：`:327` 为什么是"换判据"而不是"删"

这一处决定的是**用户原话要不要被当成行程字段解析**。原判据是 `not planning_ready`——即"行程不完整时才去解析"。这个代理量有两处不对：

- **行程已完整时，用户补充修改就走不到解析分支**（"改成去杭州出差"被静默忽略）
- **它把"要不要读用户这句话"和"行程完不完整"这两件无关的事绑在一起**

正确判据是**"解析结果与 `facts` 有无实质差异"**。这个判据是可实现的，且重放是安全的：`trip_version` 是行程内容的指纹（`store.py:20-22`，只排除 `updated_at`），`store._apply` 用 `setdefault` 保留既有 `_trip_id`（`store.py:172`），所以**同内容重写产生同版本，不会作废其他结果**。

解析函数本身是保守的，不需要动：`parse_trip_entry` 要求整句 `fullmatch` 命中 `去<目的地>出差` 且含"和/再/改成"等词即放弃（`intake_submission.py:29-37`），`parse_intake_submission` 要求逗号分段逐段命中且至少两个字段（`:41-56`）。**它不会把一句制度咨询误读成行程。**

> 施工时需实测一条：`_capability_selection` / `work_location_verified` 等写入副作用字段在幂等重放时是否保持逐字节相同。若不同，指纹会变，上面的"重放安全"结论需要回收。（`:244`/`:934-943` 表明它们由运行时写入，预计稳定，但必须验证而非推定。）

**`evaluate_trip_intake` 的残留形态**：它**不能整个删除**——`build_trip_intake_document` 仍依赖它计算 `missing_required`（`:132`）、`invalid_fields`/`conflicts`（`:133-135`）、`optional_info`/`completion`（`:160-162`）、`planning_ready`（`:167`, `:215`）。

准确的说法是**消费者从 11 处降到 1 处**：它不再是工作流驱动器，而是**卡片渲染器的内部计算**。`payload` 的 `missing` 删除后，唯一剩下的消费者只有卡片渲染这一步——而卡片渲染本来就是运行时该做的事（§2.6）。`invalid_fields` / `conflicts` 作为校验保留。

### 2.6 【修订】`request_trip_details`：把卡片决定权交给模型

§1.12 证明卡片能力只存在于运行时。因此删 gate 的同时**必须给模型一个入口**，否则是删掉能力而非交出能力。

```python
MAIN_TOOLS = [..., schema("request_trip_details",
    "向用户展示行程信息收集卡片，让用户填写出发地、目的地、日期等；"
    "在缺少行程字段且需要用户补充时调用", Empty)]
```

调用 → 运行时用当前 `facts` 渲染 `build_trip_intake_document` → 返回 `{"_terminal": {...}, "outcome": "waiting_input"}`。

**`planning_ready` 从 gate 降为该工具的渲染输入**：缺什么就在卡片上显示什么，而不是拒绝展示。`build_trip_intake_document` 本就接受部分字段，无需改动。

**连带效果**：

- 1/2/3/4 四项处置依赖此工具存在，否则卡片消失
- `engine.py:242` / `:354` 的快速路径保留为"命中即调此工具"，符合 §2.4 的职责分界原则
- `MAIN_TOOLS` 7 → 8 个

**已知风险**：模型可能想不起来调用它，卡片出现频率下降。缓解手段是**工具描述与 skill 引导**，不是运行时 gate——gate 正是这次要删的东西。此风险需在 Phase 4 的 E2E 场景中量化。

### 2.7 删除清单

| 目标 | 位置 |
|---|---|
| `PendingInput` 及其 `one_input_type` 校验器 | `contracts.py:28-43` |
| `Finish.pending_input` 字段 | `contracts.py:51-52` |
| `resolve_reply` / `previous_question` / `record_question` | `dialogue.py:23-71` |
| `can_show_intake` | `dialogue.py:74-80` |
| `render.py` 的两条 `pending_input` 分支 | `render.py:66-79` |

> `dialogue.py` 的 `clarification_output` / `CLARIFICATION`（`:12-20`）**暂留**：`waiting_for_input`（`engine.py:180`）与 `:666` 的"模型未 finish"兜底仍在用。它是**渲染出口**，不是绑定逻辑，与本次要删的那套无关。待 §3.3 处置 `complete_trip` 时一并评审。
| `pending_input` / `resolved_input` / `reply_to` / `work_context` / `choice_output` / `checkpoint_version` / `intake_submission_applied` 的全部读写点 | `engine.py:194-209, 227-244, 235-237, 275, 397, 448-449, 952, 1075, 1127-1129` |
| `guard_user_input` 的 `unclear` 判定与 `allow_short` 五项条件 | `engine.py:227-230`、`core/intent_guard.py:65` |
| `planning_ready` 的 11 处用途 | 见 §2.5 表（`:327` **换判据**，其余删除） |
| `evaluate_trip_intake` 的工作流驱动语义 | `core/trip_intake.py:128-208` **降为卡片渲染器的内部计算**（消费者 11 → 1）；函数本身保留 |
| `results()` 里的时效/版本门从**读取侧**移到**写入与交付侧** | `engine.py:741-742`（`input_version`）、`:745-751`（15 分钟证据窗）——拆成 `readable(id)` 与 `deliverable(id)`，公理 7 |
| payload 的 `missing` / `current_request` | §2.3 两个来源 |

`Finish.pending_input` 的删除**直接兑现"把推理权力交还给大模型"**：模型只需说清楚问了什么，不必把问题强行归类到 9 个枚举之一。

### 2.8 断点恢复

#### 触发：不能用 `status='running'`

实测 `supervisor_runs` 的 status 分布：**completed 67 / failed 2 / interrupted 2 / running 0**。

`running` 恒为 0 不是偶然——`store.py:78-80` 在新请求进入 `_begin` 时，把同 session 里所有其他 `running` 行**强制改成 `interrupted`**：

```sql
UPDATE supervisor_runs SET status='interrupted', cancel_requested=TRUE
WHERE user_id=%s AND session_id=%s AND request_id<>%s AND status='running'
```

所以"找最近一次还在 `running` 的行"这个查询**在你查它之前就已经被自己翻掉了**，永远返回空。

正确的语义 `_previous`（`store.py:118-123`）**已经实现**——它按 `created_at DESC LIMIT 1` 取同 session 最近一条，**根本不看 status**。缺的不是查询，是 `is_continue(user_text)` 那道门（`engine.py:210`）——只有用户字面打出"继续/请继续/继续处理/继续刚才的任务"才恢复。

**目标：去掉 `is_continue` 门。任何新请求，只要 `_previous` 返回非空 checkpoint 就进恢复。**

#### 记录已经在，且很大——问题是"丢了"不是"没有"

| 存在哪 | 内容 | 实测 |
|---|---|---|
| `state["children"][key]` | `{result_id, role, task, version, round, sources[], read_ids[], messages[]}`（`engine.py:1078-1081`）——子 agent 完整转录，含每次工具调用的返回 | 非空 **38/71** 次，最大 **194,125 字符** |
| `state["results"]` | 完整 `SpecialistResult`（含 `sources[*].data` 原始载荷） | — |
| `state["main"]["messages"]` | 父转录，每次调用以 `tool_message(call, value)` 追加（`:640`）——调用与返回成对 | 最大 25,919 字符 |
| `state["work_items"]` | 执行账本 | — |

全部随 `deepcopy(self.state)` 落盘，最大 checkpoint **276,117 字符**。但 `engine.py:207` 在新轮次硬编码 `"children": {}`——**记录了，恢复时丢掉。**

#### 三档恢复

```
L1 · 立即注入上下文（小，必给）
     conversation                完整对话（含问句散文），末条 = 本轮 user_text
     work                        跨轮继承的 {result_id, role, status, stale} 索引（上限 12 条）

L2 · 恢复为"可读状态"（不进上下文，模型按需取）
     results                     完整 SpecialistResult
     children[*].messages        完整调用/返回记录
     children[*].sources         → SourceScope 重建（:1088-1089）→ read_source 可用
     source_bytes                从恢复的 children 重新计算（:225 已是此逻辑，不用改）

L3 · 有意丢弃
     main.messages 转录           由 L1 的 conversation + work 索引替代
```

**不新增任何状态字段。** 改四处：

1. 去掉 `is_continue` 门（`:210`）——**继承成为默认行为，不再是"用户说了继续"才触发**
2. `results` / `work_items` / `children` 无条件从上一轮 checkpoint 载入（`:203-207, :210-215` 的整块重置与条件恢复合并）
3. `source_bytes` 重算（`:225` 已是此逻辑，不用改）
4. `work` 索引加上限（§2.3 点 7）

第 2 条是本次修订的核心：现行代码是**先全丢掉、再在 `is_continue` 时说一声"恢复"**——`"children": {}` 与 `"work_items": {}` 写在初始化字面量里（`:203-207`），恢复逻辑在 `:210-215` 把它盖回来。这个"丢-再捡"的形状正是 checkpoint 记录的 194,125 字符与模型看到的上下文之间那道断层的来源。

关键：**不能全量注入。** `children` 最大 194,125 字符，而模型上下文上限 80,000（`engine.py:566`）——截断只会让上下文从中间断掉，比不喂更糟。L2 用的正是现成的机制：`SpecialistResult.brief()`（`contracts.py:224-227`）是索引，`read_result`（`:909-910`）与 `read_source`（`services.py:186-201`）是按需读。**把全部记录恢复成可读状态，让模型自己取**——这比运行时挑好摘要给它更符合"把推理权交还模型"。

#### 15 分钟证据窗口：由公理 7 自动解决

15 分钟窗口在现行代码里**咬两次**，位置不同：

| 位置 | 作用 |
|---|---|
| `source_fresh`（`engine.py:301-311`），经 `restore_results`（`:211`） | **是否把结果恢复进 `results`**——过期的直接不进内存 |
| `results()`（`:743-751`） | **能否读取与交付**——`read_result` 直接 `ToolRejected` |

第二处是问题所在：`read_result` 走 `results()`（`:909-910`），所以一条 16 分钟前的车次记录，**模型连看都看不到**。于是 `children` 里有记录、`results` 里没有条目——索引与存储不一致，模型无从知道发生了什么。

**公理 7 直接消掉这个决策点**，不需要在三个选项里挑：

- **读取侧（`read_result` / `read_source`）：只看存在性**，不受 `input_version` 与 15 分钟约束。过期信息照样可读。
- **写入与交付侧（`apply_changes` / `finish` / `deliverable_results`）：两道门原样保留。** 过期的车次不能写进行程、不能交付给用户。

即"证据时效"从**能否读取**降级为**能否采信**，判断权交回模型——这不是一个需要拍板的策略选择，而是公理 7 的必然推论。原三选项中的 2 就是这个结果，但它现在不需要被单独"定策"。

代价是 `read_result` 的返回需要**携带时效状态**（`stale` / `expired` 两个计算位），否则模型读到一条过期数据却不知道它过期——那就从一个可见的拒绝换成了一个不可见的陷阱，更糟。**这是本方案对 `read_result` 唯一的契约变更。**

#### `restore_results` 的收窄

现行依赖 `checkpoint_version == 2`、`discarded_results`、`input_version`、`applied_results`、`is_valid` 五重条件。

**删 `checkpoint_version` 可证明安全**：legacy（v1）checkpoint 里结果的 `input_version` 是默认值 **0**（`contracts.py:221`），而 `version` 是 `trip_version()` 的 12 位 hex，**不可能是 0**。条件 5（`input_version == version`）**已完全覆盖**条件 1 的作用，删除不丢任何保护。

### 2.9 迁移方案

| 阶段 | 动作 |
|---|---|
| M1 | 注入改为 §2.3 的四字段 shape：`trip`→`facts` 改名 + 白名单、`work` 合并 + `stale`、`session_summaries`/`work_context`/`previous_work`/`resolved_input`/`missing`/`current_request` 全部移除 |
| M2 | `conversation` 预算化（§2.4）：去掉 8×1400，改总预算 + 最近一轮完整；**装配末尾把末条 user 行替换为 `user_text`**（§2.3 不变式 1） |
| M3 | 新增 `request_trip_details` 工具（§2.6） |
| M4 | 删 10 处 `planning_ready` gate + `can_show_intake`（**依赖 M3**）；`:327` 单独处理 |
| M4b | `:327` 判据从 `not planning_ready` 换成"解析结果与 `facts` 有实质差异"（§2.5 专条）；**先实测写入副作用字段的幂等性** |
| M5 | 删除 `pending_input`/`resolved_input`/`reply_to`/`choice_output`/`work_context`/`checkpoint_version` 及其全部读写点 |
| M6 | 删 `PendingInput` / `dialogue.py` 三函数 / `Finish.pending_input` / `render.py:66-79` 分支 |
| M7 | 删 `allow_short` 五项条件与 `guard_user_input` 的 `unclear` |
| M8 | 断点恢复：去掉 `is_continue` 门；`results`/`work_items`/`children` 无条件载入；`source_bytes` 重算 |
| M9 | `results()` 拆成 `readable(id)` 与 `deliverable(id)`（公理 7）；`read_result` 返回增加 `stale`/`expired` 两个计算位 |
| M10 | 历史 checkpoint 不做兼容（`supervisor_runs` 有 `retention_until`）；旧行缺 `facts`/`work` 即视为空 |

**施工顺序的硬约束**：

- **M4 依赖 M3**——没有 `request_trip_details`，删 gate 会让卡片能力消失（§1.12）
- **M2 先于 M5**——M2 是 §3.1 的验证前提；若验证失败需要加回一个 `text` 字段，M5 的删除面要相应收窄
- **M9 先于或同时于 M8**——M8 让 `work` 跨轮继承，如果 `read_result` 还守着 15 分钟窗口，继承回来的差旅结果会出现"索引里有、读不到"的不一致（§2.8）。这两步不能分开上线
- M5/M6/M7 是纯删除，风险集中在测试锁

**建议把 M5 拆成 per-key 提交**，每删一个键就跑一次 A 类全量（§2.11），而不是一次删完再修测试——否则失败面无法归因。

### 2.10 每个改动点 → 测试场景映射

| 改动 | 场景 | 新增/调整 |
|---|---|---|
| `conversation` 预算化 | 长交付后问句仍可见 | **新增**：构造 `Report.summary` > 1400 的 ask 轮，断言下一轮注入里问句完整 |
| `work[].stale` | 行程变更后旧结果 | **新增**：`apply_changes` 后断言其余结果 `stale=true`，且 `read_result` 的拒绝理由与之一致 |
| `work[].status` 用 `Report` 词表 | 索引与 `read_result` 一致 | **新增**：断言 `set(索引词汇) == set(Report.status)` 且同 id 两处取值相同 |
| 删 11 处 gate | 裸"出差"、缺料规划、表单提交、补充修改 | 改 `test_intent_guard.py`、`test_dialogue_routing.py`；`test_trip_intake_experience.py` **不受影响**（§2.11.B） |
| `request_trip_details` | 卡片仍能出现 | **新增**：模型调用后返回 `presentation_document.type == "trip_intake"` |
| 删 `allow_short` | 乱码、新请求打断、取消 | 改 `test_dialogue_routing.py:38-55` |
| 删 `PendingInput` | 回答+新请求、更改答案 | 新增 |
| 中断路径形状一致 | 中断后恢复 | 新增（覆盖 `cdd74610` 形状） |
| **payload 不变式 1**：`conversation[-1].content == user_text` | 表单提交、附件、普通轮 | **新增（回归测试）**：`trip_input` 提交一轮后，断言注入 payload 末条 user 行含表单 JSON；**这是 §3 待确认项 2 的验收** |
| **payload 不变式 2**：`work` 跨轮继承 | 断点恢复 | **新增**：`is_continue` 门删除后，普通追问轮也能在 `work` 里看到上一轮的 `result_id` |
| **payload 不变式 3**：`work[].stale` 在 `apply_changes` 后仍正确 | 行程变更 | **新增**：`apply_changes` 回执的 `stale_results` 与下一步 payload 重算的 `stale` 一致 |
| **payload 不变式 4**：`read_result` 永不拒绝（公理 7） | 过期证据、旧版本结果 | **新增**：对 16 分钟前的车次结果，`read_result` 成功且返回 `expired=true`；同一结果 `finish` 仍被拒 |
| 中断轮的问句可见 | 中断后恢复 | **新增**：`turn N+1` 的 `conversation` 含 `turn N` 的问句，**不依赖 turn N 的 status** |
| `work[].result_id` 必填 | 中断后恢复 | **新增**：恢复后 `read_result` 能读到中断前的完整结果 |
| 南京案例 | 复现 | 新增：`"北京"` 两轮绑定 |

**特别注意**：`tests/test_trip_entry_recovery.py:70` 锁定「空 `question` + 无 `pending_input` 的 `ask` 必须合法」，`engine.py:179` / `control.py:54` 由运行时在 Python 里构造 `Finish(kind="ask")`。§2.12 删除不变式后**该测试的前提不再成立，但它仍必须显式改写，不能默默删掉**——它同时锁定了"字段齐全时出表单"这一正常路径，而该路径现在改由 `request_trip_details` 承载。

### 2.11 测试锁清单（Phase 3 的施工边界）

`tests/` 下 67 个 `test_*.py`，541 个测试函数。按"是否会因状态重构而失败"分三类：

**A. 直接锁死 checkpoint 键名或就地构造 `state` 字面量（必须同步修改，共 13 个文件）**

| 文件 | 锁点 |
|---|---|
| `test_execution_plan.py`（12 项，**最重**） | `:22` 字面量 `state = {"work_items":{}, "results":{}, "applied_results":[]}`；`:35-72` 逐字段断言 `work_items[k]["status"/"attempts"/"finished_at"]`；`:145,205-206` `checkpoint["public_plan"]`；`:154,168,186` `result["public_plan"]["steps"]` |
| `test_dialogue_routing.py`（16） | `:203-204` 字面量 `turn.state`；`:259-260` `checkpoint["main"]["last_error"]["code"]`；`:321-322` `skills_read` |
| `test_trip_choices.py`（17） | `:218` 字面量 `turn.state = {"trip": …}`；`:221` `_terminal`；`:257-258` |
| `test_trip_entry_recovery.py`（7） | `:88-91` 字面量 `turn.state`（含 `work_items`）；`:30` `public_plan["steps"][0]["status"]`；`:75-78` |
| `test_supervisor_control.py`（23） | `:69` `checkpoint["trip"]["destination"]`；`:111-112` `children[*]["operations"]`；`:130-131` |
| `test_complete_trip.py`（7） | `:67` `main.skills_read`；`:151-156` |
| `test_supervisor_runtime.py`（10） | `:203` `checkpoint["results"]`；FakeStore 原样存档（`:89`），store 调用签名变更即断 |
| `test_supervisor_context_bounds.py`（20） | `:93` `checkpoint["results"]` |
| `test_dialogue_routing_live.py`（1，live） | `:41-53` `checkpoint["main"]`/`["children"]` |
| `test_supervisor_live.py`（2，live） | `:101` |
| `test_supervisor_live_control.py`（1，live） | `:56-70` |
| `test_execution_plan_live.py`（1，live） | `:34-39` |
| `test_supervisor_postgres.py`（1，真实 PG） | `:37` 位置参数签名 `store.call("apply", …, receipt["version"], …, "update")` |

**B. 锁死内部函数契约（同一重构面，但不是 `state`）**

- `test_trip_intake_experience.py`（18）：`evaluate_trip_intake` 的**完整返回字典形状**——`missing_required`、`missing_info`（legacy 别名）、`optional_info`、`completion`、`conflicts[*]["key"]`、`invalid_fields[*]["key"]`（`:37-88`），以及 `apply_trip_intake_defaults` 的**就地修改**语义（`:30-37`）。
  **二次评审后此文件风险下降**：`evaluate_trip_intake` 本身保留（§2.5 专条），签名与返回形状不变，测试**无需改写**。变的只是消费者——但该文件测的是函数契约，不是消费者，所以它反而成了这次重构里少数**天然安全**的锁。
- `test_intent_guard.py`（10）：`GuardResult.intent/should_call_skill/clarification`。**§2.7 删除 `unclear` 会直接命中此文件。**
- `test_evaluation.py`（14）：`evaluation.models` 的 `TurnEvaluationMetadata` / `ExecutionSnapshot.terminal_state` / `TurnEvaluationFacts`，以及 `collector._terminal_state` 语义（`:161-179`）。
- `test_single_runtime.py`（3）：`:62` 断言运行时对象的属性集**精确相等**。

**C. 只锁用户可见行为（安全，重构不应触碰）**

`test_quick_trip.py`、`test_intake_lifecycle.py`、`test_place_information.py`、`test_train_query_backend.py`、`test_journey_map_integration.py`、`test_llm_response_helpers.py`、`test_observability.py`、`test_execution_budget.py`、`test_runtime_thinking.py`，以及全部 auth / RAG / memory / webui 套件。

**`evaluation/` 不受影响。** 它对 `checkpoint` 的 grep 命中为 **0**：`collector.py` 只读 turn 输出（`response`/`answer_document`/`presentation_document`/`agents`/`interrupted`/`idempotent_replay`）与扁平的 `SpecialistResult` 摘要；`reconciler.py:48-78` 从**持久化的 chat 行**重建 `terminal_state`，不从 checkpoint。只要 turn 输出键与 `SpecialistResult` 字段存活，状态重构就穿过它。唯一的内部耦合是 `evaluation/facts.py:7` 自带一份 `TRIP_FIELDS`（用 `purpose` 而非 `trip_purpose`，与 `agent_runtime/validation.py:13` 不一致）。

### 2.12 状态机

现状把三层状态混着用。目标对三层的处理：

| 层 | 载体 | 目标处理 |
|---|---|---|
| **A. Turn 生命周期** | `control.status` / `control.outcome` | **收窄语义**（下面） |
| **B. 跨轮交接** | ~~`open_question` + `goal`~~ **对话散文** | **【修订】无结构化载体，不进状态机** |
| **C. 单步执行** | `work_items[k].status` | **原样保留**，不动 |

#### A 层

```
                       ┌─────────────┐
                       │   running   │   outcome = None
                       └──────┬──────┘
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
       ┌────────────┐  ┌─────────────┐  ┌──────────┐
       │ completed  │  │ interrupted │  │  failed  │
       └──────┬─────┘  └─────────────┘  └──────────┘
              ▼       (RuntimeStopped / 栅栏丢失 / 硬取消)
     outcome ∈ { completed, partial, degraded, waiting_input, cancelled }
```

**【修订】初稿在此声明的不变式 `control.outcome == "waiting_input" ⟺ state.open_question ≠ None` 整节删除。**

删除的连带效果：初稿为维持该不变式而列出的"三处修正"中，

| 初稿的修正 | 修订后 |
|---|---|
| 1. `settle`（`execution_plan.py:78-83`）把 needs_input 降级为 `waiting_input` 但没向用户提问 | **降级为文案问题**。无人再据此判定"是否在等回答"，label 误导性仍在但不构成缺陷 |
| 2. `degraded_output`（`control.py:54`）用 `Finish(kind="ask")` 渲染通知 | **降级为文案问题**。`render()` 会给它加"需要补充的信息"标题，语义不精确，但不再是结构问题 |
| 3. `finish` 设字段与 `:278` 更新 outcome 之间存在窗口，中断会留下违反不变式的状态 | **彻底消失**。没有字段就没有竞态 |

即：**用删除字段消掉了三处同步/修正需求，而不是用一条不变式去约束它们。** 这与"能简单就简单"的判据一致。

#### B 层 —— 无状态机的交接

```
   上一轮 assistant 散文（含问句）        ← 唯一载体，存于 conversation_messages
            │
            │  注入 payload = §2.3 的 shape
            ▼
   ┌──────────────────────────────────────────────────┐
   │  模型自己判断这句话与上一问的关系：                │
   │    · 是回答上一问 → delegate(trip_context, 填字段) │
   │    · 是新需求     → 走自己的路由                   │
   │    · 是乱码       → finish(clarify)                │
   └───────────────────────┬──────────────────────────┘
                           ▼
                finish(kind = ask | clarify)  → 新问句进入散文
                finish(kind = answer | refuse) → 本轮无问句
```

Phase 4 场景 1–6（回答 / 乱码 / 打断 / 回答+新请求 / 改答案 / 取消）**全部落在同一格里**——它们不是六个分支，是模型看到问句和原话后的六种自行判断。运行时没有任何分支可写。

#### C 层

```
pending ──→ running ──┬──→ completed
   │                  ├──→ partial
   │                  ├──→ needs_input
   │                  ├──→ failed
   │                  └──→ cancelled
   ├──→ skipped    (settle: waiting_input)
   └──→ cancelled  (settle: cancelled)
```

---

## 3. 待确认

### 本轮已关闭的两项

| 原待确认 | 结论 | 依据 |
|---|---|---|
| 原 2：表单的结构化字段没有进入 `conversation` | **关闭**——不加字段，装配时把末条 user 行替换为 `user_text` | §2.3 不变式 1 |
| 原 4：15 分钟证据窗口的定策 | **关闭**——不是策略选择，是公理 7 的推论 | §2.8、§2.1 公理 7 |

### 仍未决

1. **问句能否在预算化的 `conversation` 下稳定到达模型（§2.4）。**
   这是唯一决定"要不要退回一个 `text` 字段"的问题，**也是唯一必须在施工中实测而非推演的事**。验证方式：构造一个 `Report.summary` 超过 1400 字符的真实完整交付轮（车次 + 天气 + 制度 + 规划），检查下一轮注入里问句是否完整。
   - 能 → 确认不加字段，本方案闭环
   - 不能 → 加回**恰好一个 `str`**（不是 `PendingInput` 那套枚举 + `resolve_reply`），并在 §2.3 声明它的来源与生命周期

2. **`complete_trip` 与 `prepare_options` 的硬编码流水线（§1.10）尚未处置。**
   它是比 11 处 gate 更大的一块：写死 `policy_rag → trip_planner → [compliance]` 的顺序与中文 task 文本、伪造 `travel_info` 证据、由运行时计算业务终态。**只删 11 处 gate 而不碰它，模型的自主权只回来一半。**
   本方案建议：先落地 M1–M10，再单独评审这一块，不要合并施工。

3. **`:327` 换判据后的幂等性实测**（§2.5 专条）。
   需要验证 `_capability_selection` / `work_location_verified` 这类运行时写入的副作用字段，在"用户重复说同一句行程"时是否逐字节保持。若它们随每次写入变化，`trip_version` 指纹会变，同内容重写也会作废其他结果——那么"重放安全"这个前提不成立，`:327` 需要另想办法。

4. `session_summaries` 是否存在**仓库外**的写入方（运维 cron / 独立 worker）。若有，§2.2 的删除结论需要回收。

### 关于这份文档自身

三轮评审里，`planning_ready` 的调用点数字改了三次（5 → 9 → 11），另有一处"消费者是谁"判断错了一次（§2.5）。两次都是**靠重新读代码而非靠推理**纠正的，而且两次都是同一个模式：**运行时替模型做的决定，比设计者的印象里多。**

如果这份文档只有一个可迁移的结论，是这个：**在"把能力还给 LLM"这类改造里，不能用印象估计运行时的越权规模——必须逐处 grep 到调用点。** 数字每估一次就少一次，说明这类判断天生倾向于被低估。
