# 记忆系统阶段 2A 修改报告：画像事实与冲突确认底座

> 日期：2026-07-31
> 状态：已完成代码、数据库迁移、单元测试、真实 PostgreSQL/Redis 集成测试和完整回归
> 本轮边界：只实现阶段 2A 数据底座，不切换线上偏好交互，不实施旅行任务状态机

## 1. 修改结论

本轮将记忆系统从“偏好键值直接覆盖”向“有来源、可版本化、冲突需确认”的模型推进了一小步。

新增能力包括：

1. 只允许经过字段目录审核的画像字段进入新存储；
2. 首次明确表达可以创建第一版画像事实；
3. 相同值重复写入不会新增版本；
4. 新值与当前事实冲突时只创建待确认变更，不覆盖旧值；
5. 用户确认后，旧事实标记为 `superseded`，新事实以更高版本生效；
6. 用户拒绝后，旧事实继续保持有效；
7. 同一待确认请求可以安全重放，不会重复创建事实版本；
8. 所有关键状态切换都在 PostgreSQL 事务内完成。

本轮没有把新能力直接接入 Agent 对话流程。现有 `user_preferences` 兼容路径仍然生效，
因此线上行为没有突然变化，也不会出现“数据库已经改了，但确认回复还没有路由”的半完成状态。

## 2. 为什么本轮只做阶段 2A

完整阶段 2 同时包含两个相对独立且风险较高的领域：

- 用户画像事实、冲突确认和确认回复路由；
- 当前旅行任务状态机、乐观锁和完成事件化。

如果一次同时修改数据库、偏好 Agent、意图路由、最终回答拼接和旅行任务生命周期，
出现问题时很难判断错误来自哪一层，也难以独立回滚。因此本轮按以下边界拆分：

```text
阶段 2A（本轮）
字段目录 → 事实版本 → 冲突请求 → 确认/拒绝事务

阶段 2B（下一轮建议）
兼容数据迁移 → Agent 写入接入 → 确认问题 → 下一轮确认优先路由

阶段 2C（后续独立实施）
travel_tasks → 状态机 → 乐观锁 → 完成/取消退出默认上下文
```

该拆分保证阶段 2A 可以独立测试、独立部署，且不会改变用户当前使用方式。

## 3. 修改前的问题

当前生产兼容路径使用 `user_preferences`：

```text
(user_id, pref_type) → pref_value
```

`save_preference()` 使用 `ON CONFLICT ... DO UPDATE` 直接覆盖，存在以下限制：

- 没有事实来源 turn；
- 没有历史版本；
- 无法区分首次设置、自动明确写入、用户确认和数据迁移；
- 新旧值冲突时无法挂起等待确认；
- 并发读—合并—覆盖可能丢失更新；
- 任意自定义 key 都可能进入偏好存储，字段语义持续扩散。

阶段 0 的敏感信息过滤已经降低了泄漏风险，但无法解决事实版本和冲突语义问题。

## 4. 新增结构

### 4.1 字段目录

新增 `context/profile_catalog.py`，集中定义允许写入的画像字段、命名空间和值类型。

当前审核字段：

| 命名空间 | 字段 | 类型 |
| --- | --- | --- |
| `profile` | `home_location` | 单值字符串 |
| `profile` | `usual_departure` | 单值字符串 |
| `travel.preference` | `transportation_preference` | 单值字符串 |
| `travel.preference` | `hotel_brands` | 字符串列表 |
| `travel.preference` | `hotel_area_preference` | 单值字符串 |
| `travel.preference` | `airlines` | 字符串列表 |
| `travel.preference` | `seat_preference` | 单值字符串 |
| `travel.preference` | `meal_preference` | 单值字符串 |
| `travel.preference` | `budget_level` | 单值字符串 |
| `travel.preference` | `time_preference` | 单值字符串 |
| `travel.preference` | `food_preference` | 单值字符串 |

目录采用失败关闭策略：未知字段不自动落库，需要先明确字段语义和验证规则。
这避免为了兼容模型任意输出而把数据库逐渐变成无约束 JSON 集合。

规范化规则保持简单：

- 去除首尾空白并合并连续空白；
- 单值只允许字符串；
- 列表只允许字符串，最多 12 项；
- 列表去重但保留首次出现的展示顺序；
- 比较值忽略列表顺序和英文大小写；
- 单项最长 120 字符；
- 敏感信息和详细门牌地址继续使用统一安全边界拒绝。

### 4.2 `user_profile_facts`

该表保存画像事实的完整版本链，主要字段包括：

- `fact_id`：事实 UUID；
- `namespace + fact_key`：稳定字段标识；
- `fact_value`：JSONB 展示值；
- `normalized_value`：用于确定性比较的规范值；
- `status`：`active / superseded / rejected`；
- `write_mode`：`auto_explicit / user_confirmed / migration`；
- `source_turn_id`、`source_excerpt`：事实来源；
- `version`、`valid_from`、`valid_to`：版本和有效期。

数据库通过部分唯一索引保证同一用户、命名空间和字段最多只有一条 `active` 事实；
通过版本唯一约束防止同一字段出现重复版本号。

### 4.3 `memory_change_requests`

该表保存冲突提案，主要字段包括：

- 当前旧事实 `old_fact_id`；
- 提议值及其规范化值；
- 来源 turn 和脱敏来源片段；
- `pending / confirmed / rejected / expired` 状态；
- 到期时间和解决时间。

同一用户同一字段最多只有一个 `pending` 请求。这样下一轮确认路由始终有确定目标，
不会出现两个“是否更新常住地”的问题同时等待用户回答。

### 4.4 Profile Repository

新增 `context/profile_repository.py`，职责严格限定为：

- 查询当前有效事实；
- 查询待确认变更；
- 提交明确表达的画像值；
- 确认或拒绝待确认变更；
- 在事实真正改变时递增 `memory_versions.profile`。

它不负责调用 LLM、不负责判断用户是否明确表达，也不负责拼接聊天回复。
这些职责留给后续接入层，避免 Repository 变成混合业务流程的“大类”。

## 5. 写入与确认流程

### 5.1 首次写入

```text
验证字段和值
  → 获取用户事务锁
  → 当前字段没有 active 事实
  → 插入 version=1 / write_mode=auto_explicit
  → profile memory version +1
```

### 5.2 重复相同值

```text
规范化后与 active 值相同
  → 返回 unchanged
  → 不写新行
  → 不增加 memory version
```

### 5.3 冲突值

```text
规范化后与 active 值不同
  → 保留 active 事实
  → 创建 pending change
  → 不修改 profile memory version
```

相同冲突请求重放时返回同一个 `change_id`。如果已有另一个值等待确认，Repository 明确报错，
要求先解决旧请求，不会静默替换待确认内容。

### 5.4 用户确认

一个事务内完成：

1. 锁定用户和变更请求；
2. 确认旧事实仍是当前 active 版本；
3. 把旧事实更新为 `superseded`；
4. 插入 `version + 1`、`write_mode=user_confirmed` 的新事实；
5. 把变更请求标记为 `confirmed`；
6. 递增 `memory_versions.profile`。

任一步骤失败都会整体回滚，不会出现“旧值失效但新值未创建”的中间状态。

### 5.5 用户拒绝或请求过期

- 拒绝：变更标记为 `rejected`，旧事实不变；
- 过期：变更标记为 `expired`，新提案可以重新创建；
- 重复处理：直接返回已经持久化的结果，不重复修改事实。

## 6. 并发、一致性和安全

### 6.1 并发

Repository 使用与 Session 基础相同的 PostgreSQL 用户级事务 advisory lock，
把同一用户的首次写入、冲突创建和确认串行化。数据库部分唯一索引是第二层保护，
即使未来出现其他写入入口，也不能创建两个 active 事实或两个 pending 请求。

### 6.2 来源与最小化

- 冲突请求必须携带 UUID `source_turn_id`；
- 来源片段最多保存 300 字符；
- 来源片段写入前统一脱敏；
- 本阶段不保存完整 Agent 编排结果；
- `memory_versions.profile` 只在有效事实发生变化时递增。

### 6.3 敏感信息

新字段目录复用 `utils/memory_safety.py`：Token、密码、邮箱、手机号、证件、银行卡、
详细门牌地址和公司机密不能成为画像事实。常住城市或区县可以保存，详细门牌不能保存。

## 7. 兼容性和明确未修改内容

本轮特意保留：

- `user_preferences` 表；
- `PostgresCompatibilityStore.save_preference()` 当前行为；
- Preference Agent 输出格式；
- Onboarding 偏好写入；
- 意图识别和确认回复路由；
- Web API 和页面行为；
- `active_trip_contexts` 以及当前任务兼容逻辑；
- 文件后端的开发兼容实现。

`MemoryService` 和 `MemoryManager` 只新增一个独立的 `profile_repository` 引用；
旧 `long_term` 接口没有被替换。非 PostgreSQL 后端该引用为 `None`，不会模拟不可靠的版本语义。

## 8. 文件修改清单

| 文件 | 修改内容 |
| --- | --- |
| `context/profile_catalog.py` | 新增画像字段目录、类型和安全验证、确定性规范化 |
| `context/profile_repository.py` | 新增事实版本、冲突请求及确认/拒绝事务 Repository |
| `context/memory_service.py` | PostgreSQL 模式挂载独立 Profile Repository |
| `context/memory_manager.py` | 对上层暴露独立 `profile_repository`，不替换兼容接口 |
| `webui_new/auth/migrations/0008_memory_profile_stage2a.sql` | 新增事实表、变更请求表、约束和索引 |
| `tests/test_memory_stage2_profile.py` | 字段目录、安全边界和迁移契约测试 |
| `tests/test_memory_stage2_profile_integration.py` | 真实 PostgreSQL 版本与冲突流程测试 |
| `docs/memory-system-redesign-plan.md` | 更新阶段进度，明确阶段 2 尚未整体完成 |
| `docs/memory-system.md` | 增加当前事实源与阶段 2A 兼容边界说明 |

## 9. 验证结果

### 9.1 修改前基线

```text
阶段 0、阶段 1 和 PostgreSQL 兼容专项：24 passed
```

### 9.2 阶段 2A 单元测试

```text
7 passed, 2 integration tests skipped（未提供测试数据库时）
```

覆盖：

- 单值清理；
- 列表去重和顺序无关比较；
- 未知字段拒绝；
- 类型错误拒绝；
- 详细门牌拒绝；
- additive migration 和唯一状态索引。

### 9.3 真实 PostgreSQL/Redis 专项回归

```text
普通记忆与兼容专项：48 passed
阶段 1 + 阶段 2A 集成：11 passed
```

集成测试验证：

- 首次事实创建；
- 相同值幂等；
- 冲突不覆盖；
- 相同 pending 请求重放；
- 不同 pending 请求被阻止；
- 确认后生成 version 2；
- 旧版本变为 superseded；
- 确认重放不生成 version 3；
- 拒绝后旧事实继续 active；
- Session/Message、Redis TTL 和附件绑定原有集成行为未回归。

### 9.4 完整项目回归

```text
239 passed, 2 skipped
```

两个跳过项为现有可选外部 LLM 集成测试，与本轮修改无关。

## 10. 部署说明

迁移 `0008_memory_profile_stage2a.sql` 是纯新增迁移：

- 不删除或修改 `user_preferences`；
- 不迁移旧偏好；
- 不改变当前读取路径；
- 服务启动时由现有 checksum migration runner 自动执行；
- 已应用后不得修改迁移内容，否则 checksum 校验会拒绝启动。

部署仍使用统一 Docker 入口：

```powershell
docker compose -f docker/docker-compose.yml up -d --build hommey
```

部署后应检查：

```powershell
docker compose -f docker/docker-compose.yml ps
Invoke-RestMethod http://127.0.0.1:8000/healthz
```

## 11. 回滚说明

本轮最安全的代码回滚方式是恢复应用代码但保留新增表：

1. 不删除已经记录到 `schema_migrations` 的 `0008`；
2. 不修改已应用迁移的 checksum；
3. 旧应用不会访问新增表，因此保留表不会影响兼容路径；
4. 若将来 2B 已经写入真实画像事实，回滚前先停止新写入并保留数据；
5. 只有确认没有任何数据价值时，才通过新的前向迁移删除表，不能手工改旧迁移。

由于本轮尚未切换线上读写，代码回滚不会导致用户偏好读取中断。

## 12. 下一阶段建议

### 12.1 阶段 2B：画像接入与确认路由

建议继续保持小步实施：

1. 仅迁移字段目录内的旧 `user_preferences`，写入模式标记为 `migration`；
2. 增加新旧读取对比测试，确认规范化结果一致；
3. Preference Agent 结果必须携带当前 `source_turn_id`；
4. 空字段明确表达走 `propose_explicit_fact()`；
5. 冲突只生成 pending，并在正常回答末尾追加确认问题；
6. 意图识别前先检查 pending，用受限规则解析确认、拒绝或不明确回复；
7. 确认后更新新事实，并在迁移期刷新兼容投影；
8. 为“这次从杭州出发不等于常住杭州”增加确定性测试；
9. 通过开关逐步启用，稳定后再把画像读取切换到新事实表。

阶段 2B 不应同时实施旅行任务状态机。

### 12.2 阶段 2C：旅行任务状态机

在画像接入稳定后独立实施：

- 新建 `travel_tasks`；
- 状态限定为 `collecting / planning / planned / completed / cancelled`；
- 使用 version 乐观锁和 SQL 原子 patch；
- 完成或取消后退出默认 active 查询；
- 先双读校验，再移除 `active_trip_contexts` 兼容路径。

### 12.3 暂不进入阶段 3

可靠异步摘要、事件提取和清理 worker 应等待阶段 2B/2C 数据语义稳定。
否则 worker 会同时依赖旧偏好、兼容任务和新事实，形成更难回溯的多套状态。

## 13. 当前验收状态

| 项目 | 状态 |
| --- | --- |
| 字段目录和验证器 | 已完成 |
| 首次明确事实写入 | 数据层已完成，Agent 尚未接入 |
| 冲突不覆盖 | 数据层已完成，兼容路径尚未切换 |
| pending change | 已完成 |
| 确认/拒绝事务 | 已完成 |
| 事实版本和来源 | 已完成 |
| 确认问题追加 | 待阶段 2B |
| 下一轮确认优先路由 | 待阶段 2B |
| 旧偏好迁移/兼容投影 | 待阶段 2B |
| 旅行任务状态机 | 待阶段 2C |

结论：阶段 2A 已形成可独立验证、可安全部署、可前向扩展的数据闭环；阶段 2 整体尚未完成。
