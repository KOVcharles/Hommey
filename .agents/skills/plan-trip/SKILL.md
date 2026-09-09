---
name: plan-trip
description: Build a company business-trip itinerary from the current trip, internal policy evidence, and available travel information. Use for route, lodging-area, work-schedule, budget, and reimbursement-preparation advice; do not use for private tourism or transaction execution.
---

# 规划企业差旅行程

由 trip_planner 角色完成，只使用明确传入的行程与专业结果。
核对工作日程、已知制度、交通、天气、酒店候选和用户排除项，再给出安排。
缺少输入时报告 missing_info，由主 Agent 决定查询或补问；不能自行调度其他 Agent。
以工作安排和可靠到达为先，保留通勤与换乘缓冲。酒店参考消费不等于可预订房价。
车次、余票、金额必须来自输入资料；没有真实车次就不编造车次编号。
data.itinerary.days 每天包含 date 和 activities 文本数组，data.decision_basis 说明选择依据。
方案生成不代表真实出行完成，不自行断言整体合规，不执行任何交易。
