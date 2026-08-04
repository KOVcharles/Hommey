---
name: preference
description: Record or update the current user's business-travel preferences, including hotel brands, airlines, home location, and seat choices. Use when the user explicitly states, appends, or replaces a travel preference.
---

# Preference (偏好管理)

## 流程

1. 提取用户明确表达的差旅偏好，不从普通行程描述中推断长期偏好。
2. 将“还、也、另外、以及”识别为 `append`。
3. 将“搬家到、改成、现在是、换成”识别为 `replace`。
4. 首次设置某类偏好时使用 `replace`。
5. 返回结构化 `preferences` 列表，由 Hommey 协调器或调用方写入当前用户记忆。

常见类型包括 `home_location`、`hotel_brands`、`airlines`、`seat_preference`、`meal_preference`、`budget_level` 和 `transportation_preference`。

## 输出

```json
{
  "preferences": [
    {"type": "hotel_brands", "value": "汉庭", "action": "append"},
    {"type": "home_location", "value": "上海", "action": "replace"}
  ],
  "has_preferences": true
}
```

- `action` 只能是 `append` 或 `replace`。
- 用户没有表达偏好时返回空列表和 `has_preferences=false`。
- 不记录证件、支付、认证、健康等敏感信息。
