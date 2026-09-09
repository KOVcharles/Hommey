---
name: train-query
description: Query real China Railway (12306) train schedules, times, durations, fares and seat availability. Use for train-ticket (车票), train-number, high-speed-rail, or railway schedule questions; never general web search and never booking.
---

# 查询差旅车次

由 travel_info 角色调用 search_trains，参数是明确的起点、终点与 YYYY-MM-DD 日期。
read_source 回读真实结果后 report；不编造车次、时间、余票或价格。
当前适配器提供时刻和余票，不提供真实票价。保留查询时间，余票以再次查询为准。
地点或日期不明确则请主 Agent 补问；不购票、不改签、不退票。
