---
name: query-info
description: Retrieve weather and general transport context. Use for weather, route, flight, airport, or local-transport questions; never use as general web search. Rail/train schedules belong to train-query, not this skill.
---

# 查询差旅天气和通勤

由 travel_info 角色按需调用 get_weather 或 search_commute，read_source 后 report。
查询城市和日期来自用户或当前行程。地点同名时先让用户澄清，不能选城市中心冒充工作地点。
保留数据查询时间，区分预报覆盖日期与差旅日期。
不提供通用网页搜索或实时航班票价，不执行预订。铁路资料使用 search_trains。
