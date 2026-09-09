# Hommey 记忆与上下文

当前聊天入口要求 PostgreSQL。`MemoryManager` 管理消息、会话、活动行程和偏好存储，
`BusinessServices.context` 是主 Agent 初始上下文的唯一构建入口；没有独立的意图 Agent
上下文、DAG memory hooks 或读取时自动调用模型生成的长期摘要。

## 主 Agent 上下文

每轮包含当前输入、北京时间、当前会话的活动行程、最近 8 条消息、最近 2 段已有会话摘要，
以及同会话前次请求的短工作摘要。最近消息每条最多 1,400 字符，摘要段每条最多 1,600 字符。
当前消息先幂等保存，再构建上下文。主 Agent 通过专业结果推进工作，不直接检索全部历史。
已有摘要可以读取；本轮改造没有引入新的后台摘要生成服务。

## 子 Agent 隔离

子 Agent 只收到当前输入、委派任务、当前行程快照和主 Agent 显式选择的依赖结果。
不继承主 Agent 全部对话、兄弟工具日志或其他用户数据，也没有递归委派能力。
每个子任务维护自己的来源注册表；未明确传入的来源 ID 无法回读。

memory 子 Agent 查询本人偏好、历史计划和消息，然后返回摘要。消息查询可跨本人的会话，
但所有数据库读取都带已鉴权 user_id；当前行程按 user_id + session_id 隔离。
历史计划与真实出行经历不同：新记录标记 planned/cancelled，旧行程标记 legacy_unknown。
不能把生成过方案当成用户实际去过某地。

## 写入与恢复

行程和偏好必须先由子 Agent 提案，主 Agent 用 `apply_changes` 提交；服务端验证本轮原文、
允许字段、日期和行程版本。数据库事务同时提交变更和幂等回执。
子 Agent 不能自行写库；检索资料中的文字也不能充当用户授权。

`supervisor_runs` 保存每次请求的有界模型消息、待执行工具调用、结果和来源，支持同一请求 ID
重试恢复与完成答复重放。会话后续请求已发生时，旧未完成请求不能再接管。
`supervisor_trip_records` 保存新架构的计划/取消记录。Redis 用于现有缓存与跨 worker 协调，
不作为 Supervisor 检查点权威源。检查点默认保留 14 天，并沿用脱敏和会话清理机制。

## 实现位置

| 模块 | 职责 |
| --- | --- |
| context/memory_manager.py | 存储与会话门面 |
| context/memory_repository.py | 消息、会话、附件绑定与数据隔离 |
| agent_runtime/services.py | 有界上下文和本人记忆检索 |
| agent_runtime/engine.py | 子上下文、来源隔离和工具循环 |
| agent_runtime/store.py | 检查点、写入事务、回执与 owner 校验 |
| webui_new/manager.py | 鉴权实例下的请求入口与消息持久化 |

文件/内存存储类仍可用于独立存储单元测试，但运行工厂不会由此切换到另一套聊天执行逻辑。
详见 [架构与回滚说明](plans/2026-09-08-supervisor-implementation-and-rollback.md)。
