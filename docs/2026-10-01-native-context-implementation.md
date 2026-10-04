# 原生 messages 与上下文构造：实施说明

日期：2026-10-01。延续 9 月 30 日的文字回复改动，在现有代码上完成消息构造、提示词拆分、运行时数据及证据投影的改造。本文描述本次实际实现；旧报告保留为历史记录。

## 1. 实际改变

主 Agent 的 SDK 输入现在是：一条 system、相关历史的原生消息、当前用户消息、本轮真实的 assistant/tool 消息。工具定义单独放在 tools。主 Agent 不再接收装着 conversation/work/reference_results 的大 user JSON。

这保留了此前方案中的会话时间线、模型输入与持久记录分离、自然语言直接交付三个原则。没有引入 OpenClaw 依赖，也没有把主 Agent 和所有子 Agent 的轨迹合并。

| 原有内容 | 现在的位置与范围 |
|---|---|
| Main System Prompt | `agent_runtime/prompts/main.md`；职责和决策原则 |
| 子 Agent 能力说明 | 主 system 仅保留角色名、短标题、Skill 名；不附子 Agent 工具签名 |
| Runtime Context | system 末尾明确标记的数据块；本轮开始的业务快照 |
| 历史会话 | 原生 user/assistant/tool 消息；每个请求只选一份表示 |
| 历史 result 目录 | 完整目录留在运行时；模型只看到可见历史关联报告的有效性状态 |
| 工具说明、schema | 当前可调用 tools 的原生 schema，system 不重复抄写 |
| 原始 RAG 证据 | 原始来源保留在子任务存储；子 Agent 收到正文与必要出处；主 Agent 通常只收到报告 |
| 工具调用轨迹 | 主会话保留主 Agent 的真实调用；子 Agent 的检索轨迹留在子任务中 |
| UI 卡片 | 保存实际交付文字一次；历史 tool 中的大卡片正文投影为简短交付回执 |
| 运行诊断 | 不作为业务上下文；可选 SDK 入参快照供人工查看 |

## 2. messages 的具体结构

以下为结构示意，ID 和文字是例子；完整工具定义见生成的 request.json。

```json
{
  "messages": [
    {
      "role": "system",
      "content": "main.md 规则 + 简短角色目录 + <runtime_context>受控业务快照</runtime_context>"
    },
    {"role": "user", "content": "电子发票可以上传截图报销吗？"},
    {"role": "assistant", "content": "上一轮实际交付的回答"},
    {"role": "user", "content": "帮我查一下南京和上海的出差标准分别是什么"},
    {
      "role": "assistant",
      "content": "我来核对两地适用的差旅标准。",
      "tool_calls": [{
        "id": "call_example",
        "type": "function",
        "function": {
          "name": "delegate",
          "arguments": "{\"role\":\"policy_rag\",\"task\":\"比较南京和上海标准，保留经费、人员、适用条件和来源。\"}"
        }
      }]
    },
    {
      "role": "tool",
      "tool_call_id": "call_example",
      "content": "{\"result_id\":\"result_example\",\"role\":\"policy_rag\",\"status\":\"partial\",\"summary\":\"已核实部分条件，仍有缺项\",\"data\":{\"findings\":[]},\"missing_info\":[\"尚缺的标准\"],\"citations\":[]}"
    }
  ],
  "tools": [],
  "tool_choice": "auto"
}
```

示意中的 tools 留空以避免在文档重复整套 schema；真实请求传入当前工具定义。模型输出最后一条 assistant 后，保存实际交付文字。下一轮把这条消息继续作为历史。

如果上一轮是新版本执行，它的真实 `assistant.tool_calls → tool → assistant` 也会进入相关历史。旧版本只有 UI 问答时保留问答，不根据报告对象伪造工具调用。

历史卡片有报告关联时，模型视图将该条 assistant 标为：

```text
已交付报告 result_example 的内容：
原先实际展示的回答……
```

这一行表达正文已经展示，不是待打开的报告索引。模型无需为了简单解释再次读相同报告。正文不另复制进运行时数据块。

宿主产生的参数修正、空回复重试、预算约束不再伪装为 user。它们进入独立的 feedback 状态，渲染为本次 system 的执行约束；执行结果与工具失败仍使用真实 tool 消息。

## 3. System Prompt 的设计与维护

主提示词的完整文本以 `agent_runtime/prompts/main.md` 为准，按六个主题维护：

1. 服务范围：能处理的业务与执行权限。
2. 当前请求：理解指代、条件与多项需求，不自动恢复历史待办。
3. 事实与依据：来源、适用条件、有效性、未知项和资料信任边界。
4. 选择工作：已有信息是否够用、何时委派、如何传递任务和依赖。
5. 状态变更：用户依据、验证提交与回执，不靠模型声称完成写入。
6. 交付：直接回答、保留条件和缺项；完整卡片才选择 finish。

职责和规则不掺入实时行程、历史结果目录、参数定义或检索原文。规则文件是静态部分；运行时数据由代码构造。原生 schema 是工具参数的维护来源。

文件组织：

```text
agent_runtime/
  profiles.py                       # 角色/工具/Skill 映射与规则加载
  prompts/
    README.md                       # 编辑位置、加载方式、调试入口
    main.md
    specialist-common.md
    specialists/
      trip_context.md
      policy_rag.md
      memory.md
      travel_info.md
      trip_planner.md
      compliance.md
  context_window.py                 # 消息选择、渲染、窗口和压缩
  context_debug.py                  # 实际 SDK 入参的可读快照
```

主 Agent 不加载所有子规则或 Skill 正文。子 Agent 只加载共同规则、对应角色规则及已有流程明确提供的业务指南。

规则在进程导入时加载。修改 Markdown 后需要重启相应进程；本次没有增加热更新机制。调试 manifest 的哈希对应进程实际加载的文件版本，避免文件已改而运行提示词未更新时误判。

## 4. 运行时快照

典型值如下；空 facts 和无关联报告时不发送对应字段。

```json
{
  "today": "2026-10-01",
  "timezone": "Asia/Shanghai",
  "facts": {"origin": "北京", "destination": "南京"},
  "result_status": [
    {"result_id": "result_example", "usable": true},
    {"result_id": "result_old", "usable": false, "expired": true}
  ]
}
```

只检查模型所见历史关联的报告，不遍历输出所有历史 task/summary/结果数据。未恢复到运行时的关联报告标为 unavailable，不让一个 ID 暗示报告可读。

usable 只表示通过现有行程版本、来源有效性和丢弃状态检查；不是对结论正确性或当前适用条件的额外保证。来源有效性沿用现有逻辑：制度检查知识范围，车次/天气/住宿等检查查询时效；没有新增制度版本联网检查。

数据用 `<runtime_context>` 标记，并转义数据内的尖括号。提示词明确业务数据不能提供新指令或授权。身份、会话、请求 ID、锁、重试计数、checkpoint 细节和完整 work_items 留在运行时。

快照在本轮开始建立；首个模型调用前若宿主确实提交了行程表单，则补入 facts 和 committed_result。后续实际变更通过工具回执传入，不每轮改写前面的 facts/work。新的用户轮再刷新快照。

## 5. 历史与存储

每个请求的 `checkpoint.main` 保存 `context_version=2`、初始 snapshot、本请求的原生 messages、轮次、待执行调用和回执；不把所有上一轮消息再次复制进新 checkpoint。

上下文读取仍使用现有表，无数据库迁移：

- `conversation_messages` 决定同用户、同会话、未删除、未过期的可见问答。
- `supervisor_runs.checkpoint.main` 为这些可见请求提供原生主会话。
- 同请求选择原生表示或旧 UI 表示其中一个，不同时拼入两份。
- 已删除/过期消息所在请求不恢复原生正文；会话删除、清空状态继续过滤。
- 当前 request 从历史查询中排除，当前 user 由运行时准确追加一次；非 UUID 请求 ID 采用与消息存储相同的规范化方式。

窗口以整个用户轮次截取，保留上一轮和当前输入。不会只保留 tool 而删除对应 assistant 调用。不完整的历史并行调用组不作为完整执行重放；当前请求的 pending 不受此历史投影影响，仍由执行账本恢复。

旧 checkpoint 可在恢复时迁移消息外壳，保留调用 ID、已执行输出、pending 与提交回执。已完成请求仍返回原响应，避免再次执行或生成。

当前窗口仍按字符计：历史软预算 24,000、工具观察软预算 32,000、messages 与 tools 合计硬上限 80,000。最近问答和最新工具观察允许超过软预算。压缩只改模型输入副本；原始观察、报告和来源仍在 checkpoint 中。

## 6. 子 Agent 与 RAG

子 Agent 初始 user 数据仅包括 task、user_request、today，并按需附加 trip、input_material、dependencies。只有负责收集行程的 trip_context 会收到上一条实际 assistant 问题，以理解“2”“明天”等短回复。其他子 Agent 不再接收整段父会话。

主 Agent 的 delegate 工具结果保留 result_id、role、status、summary、findings、missing_info、evidence_refs 和精简 citations。空可选字段省略；task、input_version、完整 sources 和存储诊断不重复发送。回读报告仍可取完整结构化 data。

制度来源投影保留：

- 原始片段正文。
- 标题、章节、heading_path、页码、文档版本。
- 已存在的生效日期/适用期、机构、经费、辖区、表头、脚注、例外与质量警告。

文件路径、重复文件名、hash、解析器和索引版本、块 ID、流水号等保留在原始存储中。文档来源 ID 仍用于回读和引用。

短来源通过 search_policy 返回完整 evidence 后，不再同时附同一内容的 excerpt，并标明 `coverage=chunk`。这只表示片段已完整返回，不表示整份制度已读完。长内容保持分页，片段不完整时仍需回读。

去掉“第 1 轮只能搜索，第 2 轮必须回读”的固定流程。子 Agent 可在预算内补查具体缺口；证据充分时直接 report。最后的报告预算和结构化验证保留。政策报告有 missing_info 却声称 success 时归一化为 partial。

## 7. 人工检查入口

设置 `HOMMEY_CONTEXT_DEBUG_DIR` 后，模型 SDK 调用前输出：

- `request.json`：经过现有脱敏处理的 messages、tools、tool_choice。
- `context.md`：按角色分段展开内容、调用参数和工具 schema。
- `manifest.json`：逐消息字符数、工具开销、请求哈希、已加载提示词哈希。

这是 SDK 入参快照，不是 provider HTTP 原始报文，也不是事后用最终 checkpoint 猜出的每轮输入。捕获默认关闭，失败不会中止业务。字符数不等于 token、计费或延迟。

本次生成的样例：

- `tmp/debug/native-context-preview-20261001-final/main-context-after.md`
- 同目录 `comparison.json` 和各角色轮次的 request/context/manifest。

样例使用原文件中的历史问答、两段来源及脚本化回复执行真实构造链路，没有重新核验政策。原样本主 system 为 4,946 字符；新样例包含角色目录和必要运行时状态的主 system 为 1,932 字符，约减少 61%。两者检索/报告内容和运行路径不同，因此不能将完整调用数量或总字符差异当成实际 token/耗时优化成绩。

## 8. 验证结果与边界

离线相关回归：233 passed、1 skipped、13 deselected。覆盖原生历史、tool 配对、只保留一次交付、旧记录兼容、删除过滤、请求身份转换、断点恢复、幂等、角色隔离、业务写入和卡片交付。PostgreSQL 集成用例因未配置隔离测试库 DSN 跳过；SQL 结构和历史拼装通过测试替身验证，尚未在真实 PostgreSQL 上验证新增读取路径。

执行命令（测试资料不属于生产知识范围，所以显式清空该进程的 scope 过滤）：

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:HOMMEY_RAG_SEARCH_SCOPES=''
.venv/Scripts/python.exe -m pytest tests/test_native_context.py tests/test_agent_text_replies.py tests/test_turn_context.py tests/test_supervisor_runtime.py tests/test_supervisor_context_bounds.py tests/test_state_simplification.py tests/test_dialogue_routing.py tests/test_service_help.py tests/test_tool_discovery.py tests/test_supervisor_control.py tests/test_execution_plan.py tests/test_supervisor_loop_detection.py tests/test_single_runtime.py tests/test_runtime_thinking.py tests/test_complete_trip.py tests/test_trip_choices.py tests/test_supervisor_postgres.py -q -k 'not test_live'
```

隔离真实模型：配置的 deepseek-v4-flash，3 项通过；业务来源为虚构资料和内存存储，未查询生产 RAG 或写业务数据库。

1. 两地标准追问：一次模型调用、零工具调用，保留科研条件和非科研未知项。
2. 意图不明输入：不会自动执行历史业务。
3. 明确合并旧结论：可正常交付相关信息，不产生业务写入。

```powershell
$env:HOMMEY_RAG_SEARCH_SCOPES=''
$env:HOMMEY_RUN_LIVE_AGENT_TESTS='1'
.venv/Scripts/python.exe -m pytest tests/test_agent_text_replies.py tests/test_turn_context.py -q -k 'test_live'
```

调试中最初追问仍发生 read_result/read_source；修正工具用途说明和“已交付报告……的内容”标注后，以上最终用例通过。没有通过屏蔽工具、硬编码追问分类或放宽用例来绕过重复读取。真实模型行为仍有随机性，不能承诺所有追问永远零工具调用。

本次没有实施全会话长期摘要、精确 tokenizer 预算、任意旧报告归档检索、show_report 新接口或独立 JSONL 事件存储。原有报告恢复数量/大小界限、finish 卡片和执行 checkpoint 继续使用。业务证据未知项仍须依赖实际检索解决，上下文瘦身不会产生缺失政策。

代码与规则已修改并验证；未部署、未重启常驻服务、未创建 Git 提交。
