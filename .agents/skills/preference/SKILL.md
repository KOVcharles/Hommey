---
name: preference
description: Record or update the current user's business-travel preferences, including hotel brands, airlines, home location, and seat choices. Use when the user explicitly states, appends, or replaces a travel preference.
---

# 提出差旅偏好变更

由 memory 角色处理。需要追加偏好时先查询并回读原有偏好，再合并用户明确表达的新值。
仅处理常驻地、交通偏好、酒店品牌、航司、座位、餐食和预算偏好。
在 report 的 data.preferences 提出变更，preference_sources 为每个字段引用本轮用户原文。
酒店品牌和航司用文本数组。一次临时选择不能自动变为长期偏好。
主 Agent 审阅并 apply_changes 后才算保存。不得保存身份凭证或秘密。
