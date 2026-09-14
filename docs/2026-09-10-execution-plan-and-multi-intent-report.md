# 执行状态机、公开计划与双意图查询改动报告

## 本版结果

在现有 Supervisor 和 checkpoint 上增加步骤状态机，不引入工作流框架或数据库表。`work_items` 是步骤执行的权威记录，`public_plan` 是其公开快照。进度生成、状态变更、结束收口均不新增模型调用。

“给我查一下南京天气以及相关的差旅标准”现在进入受限的组合查询入口：先登记天气和制度两个步骤，再使用现有信号量并行调用两个专业角色，分别校验和交付。任一项失败时保留另一项有效结果，由 Runtime 返回降级说明，不依赖主模型再次决定是否结束。

## 快速路由核查

修改前的 `is_direct_policy_query` 使用整句匹配，该示例不会命中单制度快速路由。因此没有证据说明此例会被该正则截断成只查制度。原有风险是：整句退回主 Agent 后，缺少针对这两个明确交付项的覆盖保证，主 Agent 可能漏委派或提前结束。

本版增加的语法有意受限：查询动作、一个城市、天气、连接词、差旅标准或制度，必须覆盖整句。“再规划行程”、条件、否定、多城市、额外日期等不匹配时，完整请求仍交给原有主 Agent。它不是通用多意图识别器；复杂请求的全面意图覆盖保证仍未实现。

各快速查询子 Agent 只收到本步骤的请求文本。主 Runtime 保留完整用户输入，原有权限、字段依据和工具结果校验继续执行。

## 状态与约束

- 每个步骤包含稳定 ID、业务标题、用途、依赖、状态、阶段摘要和真实开始/结束时间。未开始或旧记录无法确认的时间为空，不补造时间。
- 内部沿用 `completed` 兼容已有记录，公开映射为 `succeeded`；新增 pending、partial、needs_input、skipped、cancelled，已有 running、failed 继续使用。
- 唯一的常规转换入口在 `execution_plan.transition`。启动时检查依赖结果；成功/部分完成/需补充必须对应已经通过原有校验路径的结果。行程变更完成还要求提交回执。
- 依赖已核实的 partial 结果可继续，缺失或失败的依赖不得启动后续步骤。调用方仍通过现有版本/有效期检查读取依赖。
- 超时、取消、失败后收口所有活跃步骤；尚未执行的必需步骤不能被算成任务完成。等待用户补充时停止本轮调度。
- 保留已有调用预算、失败去重和报告修复限制，没有新增无限自动重试。相同请求被显式恢复时，取消的步骤可在原 ID 上恢复一次；不重新创建同一步骤。新消息“继续”沿用已有安全结果恢复逻辑。
- 先保存 checkpoint，再发布公开快照。公开内容使用运行时模板，不发送提示词、原始检索资料、内部异常或未经验证的模型摘要。
- revision 单调递增。持久化快照递增 2，中间值供读取被数据库 fencing 停止的旧执行时投影终态，确保下一所有者恢复后发出的版本仍较新。

## 界面与恢复

新增独立的 `execution-plan.js` 和 CSS，现有 `app.js` 只增加事件接入与恢复逻辑。运行时显示并行步骤，完成后折叠为“处理过程”，不随临时分析标签删除。通过 textContent 渲染；同一 run_id 只接受较新的 revision。

`done` 在各输出分支统一携带 outcome、stop_reason 和最终 public_plan，幂等重放也能显示最终计划。连接异常时显示“进度待确认”，前端不会自行宣称业务已经取消或成功。

新增带现有路径用户鉴权的会话查询接口：`GET /api/{user_id}/sessions/{session_id}/execution-plans`。数据库按用户、会话和保留期过滤，只选择公开计划，最多 50 条。重新打开会话时恢复这些快照。此版没有增加自动轮询或断线后继续执行机制；流式断连仍沿用取消后台任务的现有策略。进程突然崩溃且尚未被后续执行 fencing 的运行记录仍可能保留 running，需要后续接管/恢复判断，不能把静态快照当作存活证明。

## 修改范围

新增核心逻辑：`agent_runtime/execution_plan.py`、`agent_runtime/fast_routes.py`。

现有模块仅做必要接入：contracts 补步骤字段；engine 在登记、开始、结果验收、终止处调用状态机并接入组合入口；store 支持停止时保存检查点和公开快照读取；manager/route 传输和读取公开计划；模板加载新组件，app 接收事件。

没有修改本轮的模型客户端、RAG 检索适配器、记忆存储业务、结果渲染模块或模型配置。仓库中这些文件已有的修改来自前面的任务，本次未清理或覆盖。

## 验证

1. 离线回归共 **120 passed**，覆盖已有控制器、表单、上下文限制，以及新状态机、并行双意图、单侧失败、超时、断连、作用域查询和恢复版本顺序。
2. Node DOM 合约测试通过：同时显示两步、乱序/重复事件不回退、文本安全渲染、终态折叠、断连不会伪造业务状态。`node --check webui_new/static/app.js` 通过，`git diff --check` 通过。
3. 真实配置模型 + 合成天气/制度 + FakeStore，最终用例 **1 passed**：19.73 秒，6 次模型调用，最大单次输入 6,377 个序列化字符（含工具 schema，不是 token），业务工具调用天气 1 次、制度检索 4 次，写入 0 次。天气 succeeded，制度 partial，因为合成材料没有提供其他职级/交通类别标准。
4. 第一次同类真实模型用例耗时 10.34 秒，但发现天气角色把制度列成缺项。收窄子任务请求后重测，最终用例不再出现该问题。两次耗时波动说明本结果不能作为生产延迟 SLA。

真实模型用例使用的是测试政策和天气，不能用其中额度回答实际差旅问题。没有执行真实数据库、RAG 和浏览器整页端到端联调，没有部署；此前 Docker/本地数据库环境限制仍需另行解决。SQL 过滤和前端事件行为的离线验证不能替代部署环境验收。

本轮发现及修复的问题详见 `2026-09-10-agent-runtime-bugs.md` 的 AR-015 至 AR-020。之前记录的 AR-014（规划自由文本可能扩写来源中不存在的车站等事实）仍未解决。

## 测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_execution_plan.py tests/test_supervisor_control.py tests/test_supervisor_runtime.py tests/test_supervisor_context_bounds.py tests/test_single_runtime.py tests/test_trip_intake_experience.py tests/test_quick_trip.py tests/test_runtime_thinking.py -q --disable-warnings
node tests/test_execution_plan_ui.cjs
node --check webui_new/static/app.js
```

真实模型兼容测试为显式启用的 `tests/test_execution_plan_live.py`，需要 `HOMMEY_RUN_LIVE_AGENT_TESTS=1`；不属于默认离线测试集。
