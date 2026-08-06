---
name: memory-query
description: Answer questions about the current user's own saved business-trip history, active trip, and travel preferences. Use for requests such as past destinations, previous travel dates, or remembered lodging and airline preferences; never expose another user's memory.
---

# Memory Query (记忆查询)

只使用运行时提供的当前用户记忆回答问题。记忆后端可能是文件或 PostgreSQL，不依赖具体存储路径。

## 流程

1. 明确用户要查询的是历史行程、当前出差任务、偏好还是聊天摘要。
2. 只读取当前已认证用户的对应记忆。
3. 按时间或类别整理相关事实，直接回答问题。
4. 没有相关记录时，明确说明“记录中没有相关信息”。

## 可靠性与隐私

- 不根据常识补全记忆中不存在的行程、日期或偏好。
- 不读取、推断或泄露其他用户的信息。
- 不把模型生成的摘要描述成精确原始对话；需要时说明它是摘要。
- 返回自然语言回答，并保留运行时要求的结构化来源摘要。
