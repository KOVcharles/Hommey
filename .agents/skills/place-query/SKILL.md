---
name: place-query
description: Resolve mainland-China places and retrieve nearby business-travel hotel POIs from configured map providers. This is an internal capability used by query-info and plan-trip, never a user intent or booking service.
---

# 查询工作地点附近酒店

由 travel_info 角色调用 find_hotels，以明确的工作地点和城市作为查询条件。
如果返回多个地点候选，则请主 Agent 补问。不得随意选地点或把城市中心当工作地址。
read_source 回读后总结距离、区域和可用参考信息，保留 evidence_refs。
高德参考消费不是实时房价，不据此断言房间可订或符合住宿限额。
