---
name: memory-query
description: Answer questions about the current user's own saved business-trip history, active trip, and travel preferences. Use for requests such as past destinations, previous travel dates, or remembered lodging and airline preferences; never expose another user's memory.
---

# 查询本人的差旅记忆

由 memory 角色使用 search_memory 查询，read_source 回读并 report 总结。
先缩小关键词和记录类型；历史指代不明确时给出候选，让用户选择。
只可查询当前已鉴权用户，不能要求传入其他用户身份。
planned 是计划，cancelled 是取消，legacy_unknown 未证实实际出行，不能表述为用户去过。
用 evidence_refs 保留查询依据。普通偏好查询结果使用 data.preference_facts；
只有明确修改长期偏好的请求才使用 data.preferences 变更提案。
