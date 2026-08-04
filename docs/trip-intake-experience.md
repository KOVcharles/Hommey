# 结构化行程信息收集

## 目标

在生成公司差旅方案前，以结构化状态收集必要信息，避免把缺失字段拼成一行文本。用户可以逐项回答，也可以用一句自然语言一次补齐；字段齐全后，原规划工作流自动继续。

功能开关：`HOMMEY_TRIP_INTAKE_CARD`。关闭后保留原有纯文字输出。

## 职责边界

```text
EventCollectionAgent
  提取用户明确表达的行程事实

core.trip_intake
  确定必填条件、进度、无效字段、冲突与 planning_ready

TripIntakeDocument
  生成展示中立的结构化卡片数据和纯文字降级内容

trip-intake-card.js
  使用安全 DOM API 渲染，不解析或执行模型 HTML
```

LLM 输出的 `missing_info` 不作为最终依据。系统使用确定性规则重新计算。

## 必填条件

规划前必须满足五个逻辑条件：

1. 出发地；
2. 目的地；
3. 出发日期；
4. 行程时长：出差天数或返程日期二选一；
5. 出差目的。

工作地点和工作时间是选填字段，默认折叠展示。

## 状态与错误

- `collecting_required`：仍缺少必填信息；
- `needs_clarification`：日期、时长或地点无效/冲突；
- `ready_to_plan`：信息完整，可以继续规划。

普通缺失使用产品强调色。只有无效或冲突字段使用错误色，并保留其他已确认字段。

## 传输与持久化

- 流事件类型：`presentation_document`；
- 文档类型：`trip_intake`；
- PostgreSQL：`chat_history.presentation_document JSONB`；
- 同时保存 `plain_text`，供旧客户端、导出和降级使用；
- 历史会话恢复结构化卡片，旧卡片自动收起并可重新展开。

## 自动续跑

规划不完整时，Orchestrator 在事项收集后暂停。用户补齐最后字段后，继续执行政策、外部信息、行程规划和合规检查。完整规划请求使用 240 秒截止时间，并通过 NDJSON 持续发送任务状态。
