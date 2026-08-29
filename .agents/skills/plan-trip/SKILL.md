---
name: plan-trip
description: Build a company business-trip itinerary from the current trip, internal policy evidence, and available travel information. Use for route, lodging-area, work-schedule, budget, and reimbursement-preparation advice; do not use for private tourism or transaction execution.
---

# 规划合规公司差旅

## 流程

1. 调用 `event-collection` 获取结构化出差事项；缺少出发地、目的地、出发日期、行程天数/返程日期或出差目的时，只追问缺失信息，不生成行程。
2. 信息完整后，调用 `ask-question` 检索适用的公司差旅制度；默认调用 `query-info` 查询目的地天气和公开交通信息、调用 `train-query` 查询真实车次。用户明确排除天气、普通交通或车次时，只跳过对应可选能力；制度检索和合规检查不得跳过。
3. 按工作时间可靠性、门到门耗时、换乘、成本、天气和制度约束比较交通方式；`transport_recommendation.preferred` 优先引用 `all_info.train_query.results.trains` 中的真实车次（含时刻与历时）。
   同一 Agent 返回多个能力结果时，以 `all_info.agent_results` 为完整记录，并使用已合并的 `all_info.<agent_name>`，不得只采用最后一个结果。
4. 生成工作优先的日程、交通缓冲和住宿区域建议。若 `all_info.place_information.results.hotels`
   存在，只能引用其中最多三家酒店及其原始距离、评分和参考消费；不得新增酒店或金额。
5. 输出报销材料清单和缺失信息；外部信息不可用时提供路线级建议，并提醒通过官方渠道核验。
6. 调用 `check-trip-compliance` 检查拟定方案；没有适用制度证据时，仅提示需要人工确认，不输出确定的合规结论。

## 可靠性

- 不得编造真实车次、航班号、余票、价格、酒店价格或公司制度。
- 高德 `reference_cost` 只能描述为“高德参考消费”，不等同于指定日期实时房价、库存或可订状态；
  `reference_cost` 缺失时写“价格待确认”。
- 车次/时刻/余票只可来自 `all_info.train_query`；train_query 不可用或未声明车次时，`transport_recommendation` 只给路线级建议并要求官方核验。
- 没有实时数据时只提供路线级建议，并要求通过官方渠道核验。
- 没有制度证据时将相关字段标记为未知。
- 除非工作任务直接要求，否则不添加景点。
- 仅提供建议，不执行预订、付款、审批或提交。

返回符合 `schemas/output.json` 的 JSON；行程中应包含 `transport_recommendation`、`lodging_advice`、`reimbursement_checklist` 和 `missing_info`。
