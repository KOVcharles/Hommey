# 上下文与主 Agent 回答：最小修改报告

日期：2026-09-30

后续更新：2026-10-01 已在此基础上继续改造原生 messages 和提示词组织，见 [新版实施说明](2026-10-01-native-context-implementation.md)。下文保留本阶段的实现与验证记录。

## 1. 实施结论与范围

本次实现的是前一版设计的最小可用改动，集中解决“历史报告已经在上下文里，主 Agent 却缺少直接回答出口”的问题。没有实现整套原生会话存储改造。

运行代码仅修改 `agent_runtime` 下 6 个文件，Git 差异为新增 73 行、删除 41 行，净增 32 行。另新增一份回归测试文件，调整 4 份现有测试，并新增本报告。

已实现：

1. 主 Agent 可以用自然语言直接结束本轮，无须先 read_result 或 finish。
2. 从历史卡片的已有 goal_id 提取报告关联，保留历史正文一次，让模型知道它对应哪个保存结果。
3. 模型可见的运行时 JSON 去掉 3 个诊断字段，并省略为 false 的状态标志。
4. 工具调用伴随文字和最终文字都会保存，避免统一变成 content=null。
5. 工具历史压缩只修改输入副本，不再因此覆盖 checkpoint 中的原始工具观察。

数据库表结构、前端、报告 schema、工具参数、业务写入权限、幂等机制及卡片渲染保持现有接口。没有新增 gate、意图分类器、总结 Agent、模型调用阶段或依赖。

本次修改已通过离线回归和隔离真实模型验证，尚未部署、重启运行服务或创建提交。

## 2. 问题及改变后的行为

旧行为：

```text
历史 assistant 已含报告正文
→ 模型选择 read_result
→ 相同业务内容以结构化报告再次追加
→ 回读来源
→ finish 选中旧报告
→ 运行时整份再次渲染
```

现在支持的正常路径：

```text
历史 assistant 正文 + 关联报告 ID
→ 目录提供该报告当前状态
→ 主 Agent 判断现有结论足够
→ 直接回答当前追问
→ 保存文字及 terminal，返回前端
```

若现有信息不足，Agent 仍可选择 read_result、read_source 或委派查询。代码没有禁止回读，也没有通过字符串相似度删除历史回答。

本次减少的是没有必要的回读动机和强制卡片出口，并不承诺所有场景都只调用一次模型。若模型主动回读，历史正文与工具结果仍可能存在内容重复。

## 3. 逐文件修改

### agent_runtime/model_client.py

- call_model 增加 allow_text 参数，默认 False，保持旧调用和子 Agent 行为。
- 主 Agent 传 allow_text=True，使用 tool_choice="auto"。
- 子 Agent 保留 required；只有 report 一个工具时继续指定 report。
- assistant_message 接受 text，保存工具调用伴随文字；只有真实没有文字的工具消息才保留 content=None。
- 纯文字消息不产生空的 tool_calls 字段。

### agent_runtime/engine.py

- 主循环收到非空文字且没有工具调用时，保存 assistant 消息与 terminal，直接结束。
- terminal 沿用现有响应结构：response 为模型文字，answer_document 和 presentation_document 为 None。
- 工具调用和文字同时出现时，保留文字并继续执行工具，不将中间说明当作最终答案。
- 空白回复继续走原有有限重试机制，不将空白判作成功。
- 调用 compact_tool_history 前先 deepcopy，压缩只作用于模型输入视图。
- 恢复未完成旧 checkpoint 时刷新主 Agent 规则，使新文字出口与提示词一致；已完成请求仍直接返回原存储响应。

### agent_runtime/services.py

- 现有历史查询同时读取 answer_document；不新增数据库查询或表字段。
- 从 assistant 的 answer_document.sections[].goal_id 提取并去重 result_ids。
- 只使用已有结构化卡片的 ID，不从自然语言猜测报告关系；user 消息不产生报告关联。
- 原有用户、会话、删除状态和保留期过滤保持不变。

### agent_runtime/context_window.py

- conversation_window 保留 assistant 的非空 result_ids；仍完整保留最近问答和当前输入。
- 投影到模型消息时，在相关历史 assistant 正文前加一行“关联报告：…”。报告正文不复制到运行时 JSON。
- 模型视图移除 context_kind、request_id、dialogue_message_count。请求身份仍保存在运行时，不需要模型阅读。
- stale、expired、discarded、committed 仅在为真时进入模型目录；后台 work_index 及业务检查仍有完整状态。
- 规则说明：已展示且仍有效的报告结论可用于解释与比较；条件变化、矛盾、缺证据时再核查。未关联、过期或失效文字不自动升级为当前事实。

### agent_runtime/profiles.py

- 主 Agent 默认可以直接回答、比较、说明服务或追问。
- 去掉“这些回复必须走 finish”的提示，减少重复交付指令。
- 明确普通文字不会自动附带整份报告，finish 是可选的结构化展示出口。
- 保留来源、适用条件、未知项、权限范围和真实提交依据要求。

### agent_runtime/capabilities.py

- 同步上下文投影接口，移除不再发送给模型的 request_id 参数。
- 服务介绍提示允许直接文字回复，固定介绍卡片仍可选择 finish(kind=help)。

## 4. 修改后的上下文结构

模型可见运行时数据仍沿用现有工作划分，只保留四个顶层字段：

```json
{
  "facts": {},
  "today": "2026-09-28",
  "work": [],
  "reference_results": [
    {
      "result_id": "result_example",
      "role": "policy_rag",
      "task": "南京和上海标准比较",
      "status": "success"
    }
  ]
}
```

后续仍是原角色的历史问答与当前工具消息。关联报告体现在历史 assistant 内容中：

```text
关联报告：result_example
原先已经展示的报告正文……
```

本次没有把完整报告再注入 reference_results，也没有新建 verified、adopted、answer_ready 等判断字段。result_ids 是后台组装时的轻量关联，不作为额外顶层模型 JSON。

## 5. 验证结果

### 离线回归：192 passed

相关套件共 192 项通过，11 项真实模型测试在这条命令中排除。覆盖主循环、角色权限、表单、卡片、写入幂等、工具纠错、执行计划、上下文、历史复用和文字出口。

```powershell
$env:HOMMEY_RAG_SEARCH_SCOPES=''
.venv/Scripts/python.exe -m pytest tests/test_agent_text_replies.py tests/test_turn_context.py tests/test_supervisor_runtime.py tests/test_supervisor_context_bounds.py tests/test_state_simplification.py tests/test_dialogue_routing.py tests/test_service_help.py tests/test_tool_discovery.py tests/test_supervisor_control.py tests/test_execution_plan.py tests/test_supervisor_loop_detection.py tests/test_single_runtime.py tests/test_runtime_thinking.py -q -k 'not test_live'
```

新增测试重点验证：

- 报告关联穿过历史窗口，正文仅一份；user 不能通过此元数据建立报告关联。
- 简短追问一次模型调用完成，无 read_result、无卡片重播、无业务写入。
- 正常重复请求，以及 terminal 已保存但外层响应未保存时，都不会重新生成文字。
- 工具附带文字被保存，但中间说明不会提前结束本轮。
- 空白主回复不会直接完成；子 Agent 的纯文字不能替代结构化 report。
- 大工具输出在模型视图中缩减，checkpoint 中原始工具观察完整保留。

### 隔离真实模型：3 项通过

使用配置的 deepseek-v4-flash，虚构政策材料和内存测试存储；没有读取或改写生产会话数据库，也没有调用真实 RAG、天气、车次等业务服务。

1. 两地标准追问：1 次主模型调用，0 个工具调用，直接区分科研经费相同部分与非科研经费未知部分，未重播整份报告。
2. 无明确意图的短输入：仍进行澄清，没有查询业务或保存数据。
3. 明确要求合并两份历史结论：可以正常交付相关结论，没有执行业务写入。

执行命令：

```powershell
$env:HOMMEY_RAG_SEARCH_SCOPES=''
$env:HOMMEY_RUN_LIVE_AGENT_TESTS='1'
.venv/Scripts/python.exe -m pytest tests/test_agent_text_replies.py -q -k live
.venv/Scripts/python.exe -m pytest tests/test_turn_context.py -q -k test_live
```

第一条命令的 `-k live` 同时匹配到一个名称含 delivered 的离线测试，因此输出是 2 passed，实际真实模型用例只有 1 项；第二条是 2 项真实模型用例。未运行全部 11 项在线用例，尤其没有声称长污染历史的 8 项在线用例本轮全部通过。

现有两类依赖弃用提示仍会出现：pytest-asyncio fixture scope，以及 dashscope Assistants API。测试没有因此失败。

### 原始样本的离线投影对比

比较材料来自用户指定目录。使用新规则和投影函数重组首轮输入；上一轮文字与该样本最终报告逐字一致，故依据已有 answer_document 的 goal_id 补入历史关联。此过程没有调用模型。

| 指标 | 旧重建输入 | 新离线投影 |
|---|---:|---:|
| messages 序列化字符数 | 9,663 | 8,990 |
| system 内容字符数 | 5,267 | 4,946 |
| 运行时参考 JSON 内容字符数 | 948 | 606 |
| 消息条数 | 9 | 9 |
| 上一轮报告正文出现次数 | 1 | 1 |

最新用户消息保持原样。首轮 messages 减少 673 字符，约 7.0%；参考 JSON 减少 342 字符，约 36.1%。这些是字符统计，不是 provider token、计费或时延数据。

完整对比及新投影保存在：`tmp/debug/latest-hommey-turn-20260928-163843/minimal-context-comparison.json`。

旧实际会话的 5 次模型调用与新虚构场景的 1 次调用并非同一请求的在线 A/B，不能据此宣称普遍降低 80% 成本。

## 6. 保留的边界与尚未实施项

为控制改动范围，本次有意保留：

- finish、reuse_reasons 和原有卡片校验。它们只影响主动使用卡片出口的调用，普通文字出口不需要经过。
- direct_policy/direct_queries 的现有快速路径。此次首先改善进入主 Agent 循环的追问，没有重构所有业务入口。
- checkpoint 的 canonical payload 和现有历史消息查询。没有迁移为跨轮原生工具时间线，也没有新增数据库表或 schema 迁移。
- read_result 的现有完整返回结构。Agent 自主回读时仍可能重复内容；本次没有增加禁止重复或自动删改历史的规则。
- 当前字符预算、压缩策略及已有 checkpoint 大小上限。没有引入 tokenizer、语义摘要或缓存策略；保留完整工具观察后，极长运行仍受原有存储大小限制。

其他实际边界：

- 缺少历史 answer_document 的旧记录不会凭空生成关联。纯文字回答本身也不新增卡片或报告 ID，后续仍依赖保留的历史和结果目录。
- 纯文字是一次正常完成的对话输出，不通过规则猜测它是否在追问并自动置 waiting_input；需要显式表单或该机器状态时仍可使用原有工具。
- 自然语言答案由模型组织，运行时不能像报告 ID 校验那样证明每句话。事实质量仍依赖证据、状态提示与评测；实际业务写入仍受原有权限和提交校验控制。
- 评估采集仍主要跟踪本轮工具结果及 finish 选择，没有新增纯文字回答的逐句引用或历史证据采纳追踪。
- 本次保留了解析后的文字，不等于保存 provider 原始 HTTP 请求/响应；此前重建日志的证据限制仍存在。

## 7. 应用与回退

没有部署或重启。应用时按项目现有方式更新并重启运行服务，使 6 个运行文件一起生效；不需要执行新数据库迁移。

若需回退，按本报告列出的 6 个运行文件的本次 diff 成组回退，并同步回退相关测试。不要直接 reset 整个工作区：工作区原有 RAG、登录和邀请功能等改动不属于本次工作。

## 8. 设计参考

本次落实了 OpenClaw 中“允许助手文字成为最终回复”和“上下文修剪不覆盖原始历史”的部分思路，报告关联是针对 Hommey 现有存储的最小适配，不是声称已完整移植 OpenClaw。

- https://docs.openclaw.ai/concepts/context
- https://docs.openclaw.ai/concepts/agent-loop
- https://docs.openclaw.ai/concepts/session-pruning

更完整的后续方案见 `docs/plans/2026-09-30-native-session-context.md`；它是设计路线，不是本次已完成清单。
