# 高德地点与附近酒店通用能力升级设计

> 日期：2026-08-29
> 状态：实施中
> 关联设计：`docs/plans/2026-08-25-quick-trip-entry-design.md`
> 适用入口：自然语言对话、行程补全卡、快速差旅表单

## 1. 已确认的产品决策

1. 高德能力属于通用外部信息能力，不由快速差旅独占。
2. 保留现有用户意图和业务主链路，不新增 `amap_query`、`nearby_hotel` 或
   `quick_trip` intent/Goal/Workflow。
3. 第一版同时支持自然语言对话和快速差旅表单，二者复用同一个地点能力节点。
4. 第一版只覆盖中国大陆，服务端使用已申请的高德 Web 服务 API Key。
5. 完整差旅规划中，只要用户提供明确的会议或客户地点，默认查询附近酒店。
6. 第一版返回综合排序前三家酒店。
7. 高德 `cost` 只能展示为“高德参考消费”，不能称为指定日期实时房价；缺失时显示
   “价格待确认”，不得由模型补造。

## 2. 设计边界

本次升级不大修以下核心逻辑：

- IntentionAgent 的用户意图集合；
- `itinerary_planning` Goal 和 `plan-trip` Workflow 身份；
- Run/Turn/Goal/Node 状态模型；
- `WAITING_USER`、`focused_goal_id` 和 graph hash 恢复保护；
- `event_collection` 的 `planning_ready` 权威；
- RAG、车次、答案合成和合规检查主流程。

本次只增加三个可插拔层次：

```text
入口适配层
  自然语言 / 补全卡 / 快速差旅
          │
          ▼
现有 Goal 与 Workflow
          │
          ▼
place_information 内部执行 Agent
          │
          ▼
PlaceInformationService → AMapProvider
```

`place_information` 是内部执行 Agent，不是用户 intent，也不创建独立 Goal。它执行时继承
当前 `information_query` 或 `itinerary_planning` Goal 的 `goal_id`。

## 3. 通用能力模型

### 3.1 能力名称

| capability | 用途 | 默认规则 |
| --- | --- | --- |
| `place_search` | 解析园区、写字楼、客户地址或地标 | 对话明确查询地点时启用 |
| `place_verify` | 使用 POI ID 或地址重新核验 | 表单提交已选地点时启用 |
| `nearby_hotels` | 围绕已确认工作地点搜索酒店 | 完整差旅且存在明确工作地点时默认启用 |
| `route_distance` | 获取候选酒店到工作地点的距离 | 第一版以高德周边搜索距离为准 |

现有 `weather`、`local_transport` 和 `train` 保持不变。能力选择继续复用
`CapabilitySelection.include/exclude`，不增加第二套布尔选择协议。

### 3.2 规范化地点

```json
{
  "provider": "amap",
  "provider_place_id": "B0XXXXXX",
  "name": "阿里巴巴西溪园区",
  "address": "杭州市余杭区文一西路969号",
  "province": "浙江省",
  "city": "杭州市",
  "district": "余杭区",
  "adcode": "330110",
  "citycode": "0571",
  "location": {"lng": 120.027, "lat": 30.279},
  "typecode": "120000",
  "verified": true,
  "verified_at": "2026-08-29T00:00:00+08:00"
}
```

浏览器提交的 `verified` 和坐标不构成信任。服务端必须使用 POI ID 或结构化地址重新查询，
并用服务端响应重建规范化地点。

### 3.3 规范化酒店结果

```json
{
  "provider_place_id": "B0YYYYYY",
  "name": "示例酒店",
  "address": "杭州市余杭区示例路1号",
  "distance_m": 680,
  "rating": 4.6,
  "reference_cost": {
    "amount": 520,
    "currency": "CNY",
    "source": "amap",
    "realtime": false,
    "label": "高德参考消费"
  },
  "price_status": "reference_only",
  "source": "amap",
  "retrieved_at": "2026-08-29T00:00:00+08:00"
}
```

若高德不返回 `cost`，则 `reference_cost=null`、`price_status=unknown`，展示“价格待确认”。

## 4. 两条业务调用链

### 4.1 单独地点或酒店问答

```text
“阿里西溪园区附近有哪些酒店？”
→ information_query Goal
→ place_information(place_search + nearby_hotels)
→ AnswerComposer / 酒店结果卡
```

### 4.2 完整差旅规划

```text
自然语言或快速表单
→ itinerary_planning Goal
→ priority 1: event_collection
→ priority 2: rag_knowledge / information_query / train_query / place_information
→ priority 3: itinerary_planning
→ priority 4: trip_compliance
```

`place_information` 只在 `event_collection` 提供明确 `work_location` 时自动查询。地点无法唯一
确认时返回候选或未知，不擅自选择同名地点，不阻断基础差旅规划。

## 5. 快速差旅输入契约

快速表单仍复用聊天请求入口，增加严格的可选字段：

```json
{
  "message": "生成本次公司差旅方案",
  "input_source": "quick_trip_form",
  "trip_input": {
    "origin": "上海市",
    "destination": "杭州市",
    "start_date": "2026-09-02",
    "end_date": "2026-09-04",
    "duration_days": 3,
    "trip_purpose": "客户拜访",
    "work_location": "阿里巴巴西溪园区",
    "work_location_note": "访客中心3楼",
    "work_location_place_id": "B0XXXXXX"
  },
  "capability_selection": {
    "include": ["nearby_hotels"],
    "exclude": []
  }
}
```

服务端在进入意图与编排层前执行以下适配：

1. 严格校验字段长度、日期、天数和允许的 capability；
2. 将结构化字段生成一条可读用户消息，避免地点事实被自然语言来源校验删除；
3. 将结构化字段注入当前 Goal 的 entities；
4. 等待中 Run 只更新 `focused_goal_id` 对应 Goal；
5. 最终仍由 `event_collection` 调用共享的确定性 intake 规则计算 `planning_ready`。

## 6. 高德接入与安全

- 配置名统一为 `HOMMEY_AMAP_WEB_KEY`，只允许服务端读取。
- 默认 API 基址为高德 Web 服务 HTTPS 地址；测试通过依赖注入替换，不访问真实网络。
- 日志不得记录 Key、完整第三方 URL、原始坐标提交体或客户地址全文。
- 设置连接和读取超时、有限重试、并发预算、短时缓存和明确错误映射。
- 高德失败按 `on_failure: continue` 降级，不改变 `planning_ready` 和合规结果。
- 国内地点统一按高德坐标返回；第一版不承诺海外地点查询。

## 7. 排序与展示

第一版最多展示前三家。先过滤非酒店类或缺少名称/位置的结果，再采用确定性排序：

1. 距工作地点距离；
2. 高德评分；
3. 高德参考消费是否存在；
4. POI ID 作为稳定平局键。

前端和 Composer 必须展示：酒店名称、地址、直线距离、评分、参考消费或“价格待确认”、
数据来源和查询时间。不得把参考消费表述为实时房价或可订库存。

## 8. 小步实施与回滚

### Phase A：Provider 与内部 Agent

- 增加规范化模型、`AMapProvider` 和 `place_information` 内部 Agent；
- 扩展 `query-info` 的地点能力；
- 增加 mock 单元测试，不改变前端和状态机。

回滚：关闭 `HOMMEY_AMAP_ENABLED`，原天气/交通查询继续工作。

### Phase B：对话式接入

- 为 `information_query` 增加地点/附近酒店正向能力解析；
- 在 `plan-trip` priority 2 中增加可降级地点节点；
- 验证普通天气、交通、车次、制度问答无回归。

回滚：从 manifest 移除地点执行 step，不迁移 Run/Turn 数据。

### Phase C：快速差旅

- 扩展 ChatRequest 和 manager 入参；
- 增加表单、地点候选和 POI 选择失效规则；
- 复用 Phase A/B 的同一地点 Agent。

回滚：关闭 `HOMMEY_QUICK_TRIP_ENABLED`，聊天入口保持可用。

## 9. 测试与验收

至少覆盖：

- 普通天气查询不触发酒店查询；
- “某地点附近酒店”只触发地点能力；
- 完整差旅含明确工作地点时自动查询酒店；
- 没有工作地点时不以城市中心伪造附近酒店；
- 高德空结果、超时、限流、畸形响应安全降级；
- 返回结果最多三家，排序稳定；
- 缺价格时显示未知，模型不能补造；
- 伪造 POI ID/坐标不能成为已验证地点；
- 快速表单与自然语言获得相同规范化酒店结构；
- 已有 `WAITING_USER` Run 仍恢复原 Goal；
- 重复 request ID 不重复创建 Run/Goal 或重复写记忆；
- 原有意图、DAG、并发、附件、补全卡和车次测试全部通过。

## 10. Bug 记录约定

只有已经复现、具备执行证据并完成根因分析的缺陷，才在 `docs/bugs/` 新建记录并更新
`docs/bugs/README.md`。记录必须包含用户现象、复现步骤、根因、修复边界、回归测试和残余风险。
一般实施调整、未发生的风险和产品取舍只记录在本升级文档或 changelog，不虚构 Bug 编号。
