# 状态简化：代码复核与实施结果

日期：2026-09-19。对应提案：[2026-09-18-state-simplification.md](2026-09-18-state-simplification.md)。

后续南京天气/高铁实际记录审计、工具目录、错误恢复及子任务补查改动见：[工具发现与混合查询恢复](2026-09-19-tool-discovery-recovery.md)。下文保留第一阶段的实施范围和当时验证结果。

## 结论

值得修改。代码可以直接证明：短回复在进入模型前被字数规则拦截；上一问可能被每条 1400 字截断；历史结果只在固定“继续”措辞下恢复；同一行程事实同时出现在多个上下文摘要里；过期结果在读取阶段就被拒绝；`planning_ready` 控制了工具可见性、报告状态、规划委派、表单和结束路径。这些机制使模型缺少原始信息，同时限制了它选择下一步的能力。

本次已经实施状态与上下文重构，保留数据库身份隔离、写入原文依据、幂等回执、来源校验、版本/时效交付限制、取消栅栏与执行预算。没有把判断能力的恢复解释成删除所有校验。

## 不能照搬的提案结论

1. **§1.3 的“表单合成分支是死代码”不成立。** 原代码 `intake_output()` 返回非空 `presentation_document`，普通入口与表单快速路径会把它带到 `record_question()`。不能用 `render()` 返回空卡片推断所有路径都会清空卡片。生产 71 次样本的统计本次未重新连接数据库核验；不能据此证明代码从未生效。删除待答状态的依据是对话已经承载问答，结构化绑定又限制了自由表达。
2. **`trip_version()` 返回整数。** 它将 SHA256 前 12 位十六进制转成 `int`，不是字符串。历史版本与当前版本是否匹配仍在采信/交付阶段检查，不以删除 `checkpoint_version` 为理由取消版本保护。
3. **只注入 `user_text` 会丢附件。** Web 入口将表单 JSON 放入两份文本，但附件解析放在 `agent_text`。普通/表单轮的末条消息等于 `user_text`；有额外解析材料时，同一条消息追加带数据标识的 `agent_text`。写入依据仍以用户原文为准。
4. **不能原样跨轮继承执行账本。** `work_items` 的失败/次数上限与 `children` 的工具调用 ID 都具有本轮含义。把它们直接复制给新请求会让历史失败占用额度、旧工具调用命中新请求，并污染 UI 和评估。新轮继承结果与提交/丢弃标记；同请求恢复才继续原执行账本与 child 转录。
5. **仅有 role/status 的索引不足以寻址。** 同一角色可以分别查天气、车次或不同城市。索引保留 `task`，让模型选择正确的 `result_id`；不再重复携带结果摘要。
6. **不清空历史 checkpoint。** 新轮可以读取旧结果；同请求恢复会转换旧 payload、清理废弃字段，保留回执、调用 ID 和旧表单 operation identity。已完成请求继续原样幂等回放。

## 已实施

### 上下文与状态

主模型首条业务 payload 只有四个顶层字段：

```json
{
  "facts": {"destination": "南京"},
  "work": [{"result_id": "result_…", "role": "travel_info", "task": "查询南京天气", "status": "success", "stale": false, "expired": false, "discarded": false, "committed": false}],
  "conversation": [{"role": "assistant", "content": "您从哪里出发？"}, {"role": "user", "content": "北京"}],
  "today": "2026-09-19"
}
```

- `facts` 是 `TRIP_FIELDS` 白名单投影，剥离存储状态、行程 ID 和完整地图服务对象。内部持久字段继续叫 `trip`；改名本身不会减少重复或改变职责。
- 每次调用主模型前刷新 `facts/work`，避免提交变更后沿用旧的 `stale`。提交回执也给出 `stale_results`。
- 数据库最多取最近 100 条消息，再按 24,000 字符软预算保留完整消息。最近一轮问答和当前输入不做头部截断；主循环仍执行 80,000 字符硬预算。不会承诺无限长度的对话都能完整输入。
- 删除 `session_summaries` 的主运行时注入/查询，保留记忆系统自身能力。没有证据证明仓库外是否有 summary writer；本次不删除其表或写入 API。
- 新 checkpoint 不再写 `pending_input`、`resolved_input`、`reply_to`、`work_context`、`choice_output`、`checkpoint_version`、`intake_submission_applied`；不再写 `main.admitted`。表单是否已提交从本轮结果回执集合判断。
- `choice_output` 的降级能力改为从通过版本/时效校验的 `trip_options` 来源重新渲染，不缓存另一份完整 UI 输出。
- 新轮最多继承最近 24 个可容纳的完整报告，总序列化预算 350,000 字符。报告、来源与提交/丢弃标记一起保留；超出预算的整条省略，不截断证据。更早的用户可见历史通过 memory 查询。该预算是历史证据保留边界，不是完整 checkpoint 的无限归档承诺。
- 本轮模型、评估与 UI 不再把所有历史角色当成本轮执行；降级也不会倾倒无关的历史结果。

### 模型决策权

- 删除短回复/纯数字/“随便看看”等 `unclear` 路由，以及 `PendingInput`、正则答复绑定和渲染时自动补问句的结构。
- “好的 / ok / 算了 / 没事”等有上下文含义的回复也交给模型，不再固定回复欢迎语。
- 新增主模型工具 `request_trip_details`，保留表单及常驻地预填能力，由模型选择展示时机。
- 移除 `engine.py` 中所有 `planning_ready` 决策点与 `can_show_intake`，不再强制覆盖 specialist 的报告状态。
- 取消 `needs_input`/`ask_user` 触发父任务自动终止、自动规划和自动表单交付。模型可以先处理混合请求中的独立部分，再选结果补问。
- 主模型可以在首轮读业务指南，无须先完成一次委派。
- 规划可在资料不全时报告 `needs_input/partial` 和空日程；只有 `success` 必须有每日安排，避免 schema 逼模型编造日期。

### 可读与可采信分离

- `read_result` 只检查 ID 存在性及报告结构；旧版本、过期、已丢弃结果都能读，并返回 `stale/expired/discarded/committed`。
- 新增主模型 `read_source`，按已保留结果的 source ID 分页读同会话来源，返回时效/版本状态；不存在或越界的来源拒绝读取。
- `finish`、`apply_changes`、委派依赖及完整方案交付仍要求结果未丢弃、行程版本一致、动态证据在有效期内。
- 表单结果与候选查询使用请求隔离的 ID，避免新轮误复用旧表单写入。

## 保留的范围与后续边界

- `complete_trip/prepare_options` 内部的完整方案流水线、单纯制度/天气的快速路由、专业角色的查询轮数限制，本次保留。完整方案流水线现在须由主模型明确调用 `prepare_trip_options`；不再因提交表单或调用普通 `finish` 被暗中启动。若进一步拆为模型自主编排，需要单独核验真实查询成本、方案完整性和失败降级，正如原提案 §3 建议。
- `public_plan` 仍是 UI 恢复使用的持久投影。其 revision 与数据库读取/停止投影有耦合，本次不同时重写该协议。
- 同请求断点所需的 `main.messages`、工具回执、`children` 仍保留。可重放的协议记录与业务事实的重复摘要不是同一种冗余。
- 原文引用、字段类型/日期、明确取消授权、来源归属/回读、真实地点身份及写入栅栏继续由代码验证。
- `evaluate_trip_intake` 保留为表单展示计算函数；表单自己的缺项/冲突状态不再支配主模型的业务流程。

## 验证

新增 `tests/test_state_simplification.py` 并同步既有测试：长交付末尾问句、短回答进入提取模型、编号选项对话交接、完整表单/附件输入、普通追问读取历史证据、过期/丢弃可读但不可交付、提交后的索引刷新、跨轮额度隔离、旧 checkpoint 恢复、重复表单与地图副作用字段幂等、历史失败不阻断新任务、缺日期的诚实空计划。

原有要求“零模型调用”“缺字段必出表单”的测试已改为验证模型工具决策后的用户结果。幂等回放、来源归属、完整方案、场所变更及 Web 流式卡片的回归继续保留。

扩大后的可独立运行回归：**560 passed、43 skipped、3 deselected**。在核心改动完成时，相关专项回归为 **220 passed、2 skipped**；之后针对补充的确认词、数据库消息读取及评估隔离又执行了定向验证，并纳入上述 560 项通过结果。最后修正“旧提交不能把新空表单标成已保存”，补充用例与相关状态/对话/表单回归 **72 passed**。

全量测试尝试未全绿，以下限制被单独记录，而不是修改环境或删掉测试：

- `test_observability.py`、`test_webui_error_responses.py` 收集阶段依赖 PostgreSQL；本地 DSN 无法解析。
- `test_migrations_concurrency.py` 同样被 DSN 阻塞；Redis 协调/真实并发测试没有可连接的服务。
- 一个文件库测试需要 Windows 创建符号链接权限，当前环境报 WinError 1314。
- `test_rag_phase0_eval.py` 的两个质量基线断言未通过，分别是 recall floor 与 no-answer evidence；本次未修改 RAG 实现或样本数据。

最终可复现命令（使用项目 Python 环境）：

```powershell
.venv/Scripts/python.exe -m pytest -q --ignore=tests/test_observability.py --ignore=tests/test_webui_error_responses.py --ignore=tests/test_concurrency_e2e.py --ignore=tests/test_redis_coordination.py --ignore=tests/test_migrations_concurrency.py --deselect=tests/test_knowledge_base_routes.py::test_library_does_not_list_symlinks_that_escape_root --deselect=tests/test_rag_phase0_eval.py::test_baseline_recall_floor_is_sane --deselect=tests/test_rag_phase0_eval.py::test_no_answer_queries_get_no_strong_evidence
```

`git diff --check` 已通过。测试中的模型为受控桩，能证明运行时允许/阻止什么，不能替代真实模型效果评估。生产样本统计及真实模型联调未执行，也未部署或提交 Git commit。
