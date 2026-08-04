---
name: plan-trip
description: Build a company business-trip itinerary from the current trip, internal policy evidence, and available travel information. Use for route, lodging-area, work-schedule, budget, and reimbursement-preparation advice; do not use for private tourism or transaction execution.
---

# 规划合规公司差旅

## 流程

1. 调用 `event-collection` 获取结构化出差事项；缺少出发地、目的地、出发日期、行程天数/返程日期或出差目的时，只追问缺失信息，不生成行程。
2. 信息完整后，调用 `ask-question` 检索适用的公司差旅制度，并调用 `query-info` 查询目的地天气和公开交通信息。
3. 按工作时间可靠性、门到门耗时、换乘、成本、天气和制度约束比较交通方式。
4. 生成工作优先的日程、交通缓冲和住宿区域建议。
5. 输出报销材料清单和缺失信息；外部信息不可用时提供路线级建议，并提醒通过官方渠道核验。
6. 调用 `check-trip-compliance` 检查拟定方案；没有适用制度证据时，仅提示需要人工确认，不输出确定的合规结论。

## 可靠性

- 不得编造真实车次、航班号、余票、价格、酒店价格或公司制度。
- 没有实时数据时只提供路线级建议，并要求通过官方渠道核验。
- 没有制度证据时将相关字段标记为未知。
- 除非工作任务直接要求，否则不添加景点。
- 仅提供建议，不执行预订、付款、审批或提交。

返回符合 `schemas/output.json` 的 JSON；行程中应包含 `transport_recommendation`、`lodging_advice`、`reimbursement_checklist` 和 `missing_info`。
