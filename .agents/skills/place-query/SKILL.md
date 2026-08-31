---
name: place-query
description: Resolve mainland-China places and retrieve nearby business-travel hotel POIs from configured map providers. This is an internal capability used by query-info and plan-trip, never a user intent or booking service.
---

# 查询地点与附近酒店

1. 优先使用上游 `event_collection` 已提取的工作地点和目的城市。
2. 单独地点问答时，只从当前 Goal 的 scoped query 提取地点关键词。
3. 地点无法唯一确认时返回候选，不猜测、不伪造坐标。
4. 附近酒店最多返回三家，按确定性规则排序。
5. 高德消费字段只能标记为参考消费，不代表实时房价、库存或可订状态。
6. 不执行预订、付款、取消或审批。
