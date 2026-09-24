# 服务介绍与业务报告的交付契约

日期：2026-09-21。接续 [本轮上下文边界](2026-09-21-current-turn-context-boundary.md)。

## 问题与证据

北京时间 15:28:39 的请求 `bf95138a-6bb0-4667-bed3-24e476e7f144`，用户输入是“你是谁 有啥用”。当前消息没有丢失，运行容器也已经包含本轮上下文边界修改。主循环只运行两轮：

1. `finish(kind="answer")`，未选择专业报告，被“答案必须选择至少一个专业结果”拒绝。
2. 选择六份历史报告，并填写“用于说明我能处理的报销咨询范围”等复用理由；校验通过后整份报告拼接交付。

此前 15:27 的请求 `b7d103ed-7226-4a7d-b1b0-ddb32b4397bb` 也出现相同的空答案拒绝，随后交付三份历史报告。两次本轮 `work_items` 都为空。这是回答协议缺少服务介绍出口与历史结果误选叠加，不是上下文溢出或重复读取导致的轮数耗尽。

`reuse_reasons` 能记录复用决定，但不能证明理由在语义上正确；把历史标成参考也无法弥补交付协议缺少合法出口。

## 实施设计

沿用现有 `finish`，给 `kind` 增加 `help`，不新增工具、路由分类器、关键词、相关性门槛或持久状态。

| 请求 | 结束方式 | 交付内容 |
| --- | --- | --- |
| 询问助手身份、能力、使用方法 | `finish(kind="help")` | 运行时服务介绍，不要求报告 |
| 介绍助手，同时重述某项业务结论 | `help` + 对应 `result_ids` | 服务介绍与选中报告；历史报告仍填 `reuse_reasons` |
| 普通业务咨询、明确合并历史结论 | 原有 `answer` | 选中的有据报告 |
| 意图不明或任务缺资料 | 原有 `clarify` / `ask` | 澄清或补问，规则不变 |
| 超出业务范围 | 原有 `refuse` | 原有服务范围说明 |

服务介绍是运行时维护的文本，能力名称来自现有 `PROFILES`。不让模型自由填写业务结论，也不从历史报销材料推断助手能力。该文本同时用于已有问候快速路径、主模型的能力目录和最终渲染，避免多份介绍漂移。

主模型能力目录现在提供实际可交付的服务介绍，工具 schema 与主提示说明：`result_ids` 是需要完整展示的报告，不是参考资料列表；读取不等于展示；服务介绍与用户明确要求的业务结果可以一起交付。

空 `answer` 仍不能成功，但错误改为 `EMPTY_ANSWER`，用既有结构化错误机制指出 `kind/result_ids`，引导模型选择 `help`、相关业务报告或澄清，而不是只要求它补一份报告。这是原校验的纠错改进，没有增加执行前拦截。

## 为什么选这个接口

试验过在现有 `answer` 上增加可选服务介绍布尔字段。当前配置模型在长历史下多次遗漏该字段，甚至收到明确修正反馈后仍重复空 `answer`，因此未保留。

明确的 `help` 类型更符合现有 `ask/clarify/refuse` 的终止接口。其初版测试曾漏掉混合请求中的业务结论，最终补齐了 schema、实际可交付服务说明和报告展示语义，保留该失败场景作为真实模型回归用例；没有通过强制附带刚读过的报告来修复。

## 兼容性和范围

- 不修改模型上下文 payload、上下文投影、原始对话角色、历史长度或 checkpoint 结构，无数据库迁移。
- `Finish` 原字段、默认值和四个旧 `kind` 继续有效，只扩展一个枚举值并补充字段说明。
- 继续使用原有 `AnswerDocument`，服务介绍是一段现有 `notice` 类型的成功 section；前端不需要新 DTO 或分支。已有问候路径仍返回普通文本和空文档。
- `help` 中选择的报告仍经过原有存在性、版本、时效、知识范围、丢弃、变更提交和历史复用校验。
- 纯介绍不会清除旧报告。后续业务追问和用户明确要求汇总历史仍可读取、选用这些报告。
- 旧 pending 调用继续原样恢复，成功响应继续幂等回放，不追溯重写已经错误交付的历史消息。
- 服务介绍说明已注册能力与使用方式，不承诺所有外部服务此刻可用，也不回答模型供应商、模型版本或任意产品知识问题。
- 本次不改 `read_source` 哈希去重、取消释放链、RAG 检索或业务写入流程。

这消除了“介绍服务也必须找报告”的协议冲突，但不构成对模型语义判断的绝对保证。`reuse_reasons` 依然只是可审计声明，`help` 为支持明确的混合请求可以选报告；运行时没有新增一个语义分类 gate 判断用户是否真正要求了这些报告。后续模型或提示词变化应重跑真实模型用例。

## 验证方式

最终离线回归 **178 passed、10 skipped**；跳过项均为需显式启用的真实模型用例。最终真实模型回归 **10 passed**（新增八项与原有两项），保留单纯介绍至多两次调用、历史业务至多四次调用以及不能夹带无关结果的断言。使用配置中的 `deepseek-v4-flash`，保持温度 0.7 与关闭 thinking 的生产生成参数。定向 `git diff --check` 通过；测试仅有第三方弃用提示。

离线用例覆盖：带六份历史报告和 33 条对话的服务介绍；空答案修正；不查询、不写入且保留历史；混合交付只选目标报告；历史复用声明、旧版和丢弃检查；pending finish 恢复与幂等回放。既有上下文、短回复、行程字段、来源、工具发现、执行计划和循环检测回归一起运行。

```powershell
.venv/Scripts/python.exe -X utf8 -c "import os,pytest; os.environ['HOMMEY_RAG_SEARCH_SCOPES']=''; raise SystemExit(pytest.main(['tests/test_service_help.py','tests/test_turn_context.py','tests/test_supervisor_control.py','tests/test_supervisor_runtime.py','tests/test_supervisor_context_bounds.py','tests/test_supervisor_loop_detection.py','tests/test_dialogue_routing.py','tests/test_state_simplification.py','tests/test_tool_discovery.py','tests/test_execution_plan.py','-q']))"
```

真实模型用例使用当前配置端点和生成参数，历史制度与用户数据均为内存夹具，不修改生产会话。长历史故意包含重复、混合的 assistant 答案，验证原句、身份问法变体、使用方式、介绍加酒水结论、合并两项、明确合并全部六项、复述单项和无意义输入。原有两条上下文真实模型用例一起保留。

```powershell
.venv/Scripts/python.exe -X utf8 -c "import os,pytest; os.environ['HOMMEY_RAG_SEARCH_SCOPES']=''; os.environ['HOMMEY_RUN_LIVE_AGENT_TESTS']='1'; raise SystemExit(pytest.main(['tests/test_service_help.py','tests/test_turn_context.py','-k','test_live_','-q']))"
```

清空知识范围只作用于测试进程，不修改 `.env`；真实模型评估会消耗调用额度。有限样本的通过不保证所有历史、措辞和模型都通过。

## 发布与回退

本次仅更新工作区，未部署或重启服务。发布应一起更新 `contracts.py`、`engine.py`、`render.py`、`profiles.py`、`capabilities.py`，保证模型目录、工具契约、执行和渲染一致。回退仅撤销本次差异，不还原整个文件到 Git HEAD，以免覆盖此前已存在的其他修改。

旧代码不认识新 `kind=help`，回退部署前应让正在执行的新请求结束；已经完成并保存的响应仍可以直接回放。
