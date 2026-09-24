# 本轮上下文与历史结果交付边界：最小修改

日期：2026-09-21

## 问题与证据

同一会话中观察到两个不同问题：

1. 无意义输入 `asdawdasdas` 后，主 Agent 依次读取历史报告、来源和多个 Skill。该请求的 14 条请求/结果指纹均只出现一次，因此双哈希防重复机制不能识别这种缺少明确目标的探索。
2. “酒水可以报销吗”对应请求 `36440247-d025-44ac-b5bb-2075d8b81f8b` 的 `finish` 同时选择电子发票结果 `result_dbc4e5aa54ce45f6` 和酒水结果 `result_d521877a574b45ea`。运行时输出两个“制度检索”章节：前者 3 条、后者 5 条。本轮执行账本只有酒水任务。问题发生在结果选择及校验阶段，不是前端重复渲染。

此前上下文把 `facts`、历史与本轮共用的 `work`、完整 `conversation` 编成单条 user JSON。当前请求埋在 `conversation[-1]`。结果校验检查版本、时效、来源范围与提交状态，但没有区分本轮结果与历史结果的交付资格。

## 设计原则

- 本轮指一次用户请求，由 `request_id` 标识；一次 LLM 调用不是一个新用户轮次。同请求重试保留原执行状态。
- 本轮用户原文决定任务；历史问答用于理解指代；历史 assistant 文字不自动成为事实依据。
- 保留历史资料的可读性和显式复用能力，不通过字数、乱码正则或固定“继续”关键词裁决意图。
- 只修改主模型看到的临时视图，不创建另一份持久化的当前请求、目标、待答状态。
- 阅读历史不等于选择它作为本轮答案；选择历史结果必须显式声明与当前问题的关联。

## 本次实现

### 1. 模型上下文是持久状态的临时投影

`agent_runtime/context_window.py:main_model_context` 从现有 canonical transcript 派生主模型输入：

```text
system：现有规则、上下文边界约定、当前工具目录
user：runtime_reference 数据块
    request_id / dialogue_message_count / today / facts
    work                 仅本轮 work_items 对应的结果
    reference_results    其余历史结果索引，不是待办清单
历史 user / assistant 消息，保留原角色与顺序
本轮 user 消息，包含原有附件/输入解析标记
本轮 assistant 工具调用 / tool 结果 / 运行时反馈
```

本轮输入在该视图中不再嵌套于 JSON，也不再另行复制。`dialogue_message_count` 标识原始对话区域，便于检查本轮用户输入和后续运行时消息的边界。

工具修复、表单提示等原先追加为 user 的内部消息，在投影中标为“运行时反馈（不是新的用户请求）”。工具调用与结果配对原样保留。

持久化的 `main.messages` 结构不变，仍是 system + canonical payload + 本轮工具轨迹。子 Agent 的委派输入、评价采集器和断点恢复继续读取该结构。旧 checkpoint 每次进入模型调用时同样经过新投影，无需迁移。投影不写回数据库。

### 2. 显式历史复用

`Finish` 增加可选字段 `reuse_reasons: dict[str, str]`，默认空字典。运行时从已有 `work_items` 推导当前结果集合，不另存一份易失同步的清单。

仅交付本轮结果的调用保持不变：

```json
{"result_ids": ["current_wine_result"]}
```

用户要求把以前的电子发票要求一并列出时，可以显式复用：

```json
{
  "result_ids": ["current_wine_result", "historical_invoice_result"],
  "reuse_reasons": {
    "historical_invoice_result": "用户明确要求把之前电子发票的要求一起列出"
  }
}
```

约束：

- 所有选中的历史结果都必须有说明；不得为未选择的结果或本轮结果附加说明。
- 每条说明为非空、至多 300 字的字符串。
- 缺失或不匹配返回 `HISTORICAL_RESULT_NOT_ADOPTED`，引导模型移除无关结果，或显式声明必要复用。
- 原有版本、过期、知识范围、丢弃、未提交变更检查继续生效；填写复用说明不能绕过它们。
- `answer` 和携带结果的 `ask` 都执行交付边界校验；`clarify` 不允许携带结果或复用说明。
- 降级输出继续只交付本轮执行账本中的有效结果，不自动附带未采纳的历史内容。

说明仅用于工具契约和审计，不出现在用户答案中。本次不增加“采纳历史”工具或持久化采纳状态；成功 `finish` 已经是终止操作。

后续“你是谁 有啥用”误交付历史报告的原因、服务介绍出口与验证见 [服务介绍交付契约](2026-09-21-service-help-delivery.md)。本页验证数字保留为第一阶段记录。

## 兼容性与明确限制

- 不清空历史 assistant 消息，保留上一问、编号选项和纠错语境，避免破坏“学生”“第二个”“继续”等回复。
- 不更改结果存储、子任务执行、RAG 检索、业务写入或用户会话数据。
- 已完成请求仍返回原存储响应，不追溯改写历史答案。
- 未完成旧请求若正在提交没有声明复用的历史结果，会收到上述可修复错误；原 pending 调用和输出不丢失，也不重跑已完成子任务。若原预算已经耗尽，仍可能降级退出。
- 这次是报告级复用，未实现单条 finding 的选择投影。仅需旧报告一部分且完整报告会夹带无关内容时，应通过专业任务形成面向本轮的结果；不承诺自动裁剪。
- `reuse_reasons` 的存在和归属可以确定性校验，语义关联本身不能仅靠该字符串保证。模型仍可能填写不合理理由，需持续用多轮真实模型评估发现。
- 没有增加入口分类 Agent、持久目标状态、历史语义检索或全量历史摘要。历史参考目录仍有现有的数量/字符边界，尚未做相关性筛选。
- 双哈希继续识别重复请求/结果；它不负责判断每一份新资料是否服务于当前需求。
- 本次未处理 Starlette/AnyIO 断连后的资源释放问题。

## 验证

离线回归：**173 passed，2 skipped**。跳过的是需要显式启用的真实模型测试。覆盖：

- 主模型上下文中的当前输入唯一性、原生历史角色、附件、运行时反馈分隔及工具调用配对。
- 读取历史发票后不能未经声明附带到酒水 `answer` / `ask`；修正选择后只输出酒水结果。
- 有意合并历史结果、无效复用说明、过期/丢弃/旧版结果不能绕过校验。
- 旧 checkpoint 的 pending finish 可恢复并修复，已完成响应仍幂等回放。
- 短回复、编号选择、表单提交、字段修订、子任务、执行计划和双哈希防空转。

```powershell
.venv/Scripts/python.exe -X utf8 -c "import os,pytest; os.environ['HOMMEY_RAG_SEARCH_SCOPES']=''; raise SystemExit(pytest.main(['tests/test_turn_context.py','tests/test_supervisor_control.py','tests/test_supervisor_runtime.py','tests/test_supervisor_context_bounds.py','tests/test_supervisor_loop_detection.py','tests/test_dialogue_routing.py','tests/test_state_simplification.py','tests/test_tool_discovery.py','tests/test_execution_plan.py','-q']))"
```

清空知识范围只作用于这个测试进程，避免本机 `.env` 中的生产知识范围拒绝测试夹具，不改 `.env` 或生产配置。

真实模型隔离评估：**2 passed**。使用配置的模型端点、虚构制度结果和内存存储，不读取或写入生产用户会话：

1. 历史 assistant 中故意混入电子发票与酒水结论，当前输入为 `asdawdasdas`：两次模型调用以内进入澄清，无业务查询或写入。
2. 同样的历史下明确要求合并两项结论：四次模型调用以内完成，两个历史结果均显式声明复用，无业务查询或写入。

```powershell
.venv/Scripts/python.exe -X utf8 -c "import os,pytest; os.environ['HOMMEY_RAG_SEARCH_SCOPES']=''; os.environ['HOMMEY_RUN_LIVE_AGENT_TESTS']='1'; raise SystemExit(pytest.main(['tests/test_turn_context.py','-k','live','-q']))"
```

真实模型评估会使用模型额度。这两条通过记录是有限样本，不是对所有模型和历史长度的保证。后续应补充身份条件修改、主题切换、跨多轮指代和不合理复用理由的评估。

## 发布与回退

当前仅修改工作区，未重启或更新运行容器。无需数据库迁移。

应用时需一起更新上下文投影、工具契约、finish 校验和提示词，避免模型看不到新字段却被新校验拒绝。回退也应作为一组回退本次差异，保留此前双哈希改动和其他工作区修改，不能直接还原整个文件到 Git HEAD。

## 设计参考

- [LangChain：临时模型上下文与持久状态分离](https://docs.langchain.com/oss/python/langchain/context-engineering)
- [Anthropic：围绕当前任务组织必要上下文](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [OpenClaw：当前上下文与存储记忆的区别](https://docs.openclaw.ai/concepts/context)

这里借鉴上下文分层与临时投影思路；具体交付契约由 Hommey 的报告和执行账本决定，并非直接移植上述框架。
