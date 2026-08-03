# Hommey 记忆系统

本文档描述当前仓库中已经生效的记忆系统实现，包括数据如何写入、如何进入 Agent 上下文、如何按用户隔离，以及本地和 Docker 环境的存储方式。文档以当前代码行为为准，不代表未来规划。

> 2026-07-31 状态补充：Docker 生产路径已使用 PostgreSQL Session/Message 事实源和
> Redis 24 小时热缓存；版本化画像事实与冲突请求的数据底座已经加入，但现有 Agent
> 仍通过兼容 `user_preferences` 路径读写，尚未启用自动确认交互。最新实施边界以
> `docs/memory-system-redesign-plan.md` 和阶段变更报告为准；下文保留的文件后端及兼容
> 接口说明主要用于本地开发与迁移期行为。

## 1. 总体结构

项目的核心实现是“两层记忆”，并额外维护一份“当前出差任务”工作状态：

| 层次 | 主要用途 | 生命周期 | 可选后端 |
| --- | --- | --- | --- |
| 短期记忆 | 保存当前会话最近几轮对话，用于指代消解和多轮理解 | 会话级 | Python 内存、Redis |
| 长期记忆 | 保存偏好、完整聊天记录、历史行程和统计信息 | 跨会话 | JSON 文件、PostgreSQL、显式禁用 |
| 当前出差任务 | 增量保存当前正在收集的出差信息 | 跨会话，每位用户一条 | 跟随长期记忆后端 |

`MemoryManager` 是统一入口，负责为同一个 `user_id + session_id` 组装短期和长期记忆。

```text
用户请求
  │
  ├─ 读取当前出差任务、长期摘要、最近 5 轮对话
  │          ↓
  │      意图识别
  │          ↓
  ├─ 写入用户消息（短期 + 长期）
  │          ↓
  │      多 Agent 编排
  │          ├─ 读取最近 3 轮对话
  │          ├─ 读取用户偏好
  │          └─ 读取当前出差任务
  │          ↓
  ├─ 从 Agent 结果提取偏好、当前任务和历史行程
  │
  └─ 写入助手消息（短期 + 长期）
```

主要实现文件：

- `context/memory_manager.py`：两层记忆的统一门面和长期摘要。
- `context/short_term_memory.py`：内存/Redis 短期会话窗口。
- `context/long_term_memory.py`：文件/PostgreSQL 长期存储。
- `webui_new/manager.py`：Web 请求中的上下文读取和消息写入。
- `agents/orchestration_agent.py`：向子 Agent 注入记忆并回写结构化信息。
- `.claude/skills/memory-query/script/agent.py`：回答“我以前去过哪里”等记忆查询。

## 2. 身份与会话隔离

### 2.1 user_id

Web 登录成功后，JWT 的 `sub` 是 `users.id`。受保护接口要求 URL 中的 `{user_id}` 与 JWT 身份一致，否则返回 403。因此 Web 用户不能通过修改 URL 读取其他用户的记忆。

所有长期记忆查询都带 `user_id` 条件：

- 文件模式：每位用户一个 `{storage_path}/{user_id}.json`。
- PostgreSQL 模式：每张记忆表都有 `user_id`，读写都使用参数化的 `WHERE user_id = %s`。
- Redis 模式：key 为 `{prefix}:{user_id}:{session_id}`。

CLI 没有 JWT 边界，`user_id` 由启动时输入，因此 CLI 环境的身份可信度取决于运行机器本身的访问控制。

### 2.2 session_id

Web 为每个 `HommeyWebInstance` 生成一个 8 位随机 `session_id`。同一服务进程内，同一用户重复初始化会复用该实例和会话；服务重启后会生成新会话。

CLI 每次启动也生成新会话。长期聊天记录会保留 `session_id`，从而区分当前会话和历史会话。

## 3. 短期记忆

### 3.1 数据结构

每条短期消息包含：

```json
{
  "role": "user",
  "content": "下周去上海出差",
  "timestamp": "2026-07-15T22:00:00.000000",
  "metadata": {}
}
```

短期记忆按消息追加，只保留最近 `2 × max_turns` 条。默认 `max_turns=10`，即最多保存 20 条消息。

### 3.2 memory 后端

消息保存在当前 Python 对象的列表中：

- 服务重启后丢失；
- 调用 `end_session()` 或 `short_term.clear()` 后清空；
- 适合单进程本地调试。

### 3.3 Redis 后端

Redis 使用 List：

```text
hommey:short_term:{user_id}:{session_id}
```

写入时执行 `RPUSH`，随后用 `LTRIM` 保留滑动窗口。读取结果保持时间正序。

当前实现没有给 Redis key 设置 TTL。正常调用 `clear()` 会删除当前 key，但 Web 暂无“结束会话”接口；服务重启后旧 session key 不再被读取，也不会自动过期，需要运维侧设置清理策略。

## 4. 长期记忆

长期记忆对三种后端暴露相同的主要操作：

- `save_preference()` / `get_preference()`：保存和读取用户偏好；
- `add_chat_message()` / `get_chat_history()`：保存和读取聊天；
- `save_trip_history()` / `get_trip_history()`：保存和读取已形成的行程历史；
- `upsert_active_trip()` / `get_active_trip()`：增量维护当前出差任务；
- `get_statistics()`：读取累计统计；
- `clear_history()`：清空聊天和历史行程，但保留偏好与当前任务。

### 4.1 JSON 文件后端

默认路径是 `data/memory/{user_id}.json`，结构如下：

```json
{
  "user_id": "9",
  "preferences": {},
  "chat_history": [],
  "trip_history": [],
  "active_trip": null,
  "statistics": {
    "total_trips": 0,
    "total_messages": 0,
    "total_queries": 0,
    "frequent_destinations": {}
  }
}
```

文件写入采用“先写 `.tmp`，再原子替换”的方式。读取到损坏 JSON 时，原文件会被移动为 `.broken-<随机值>.json`，随后创建空记忆文件。`data/memory/*.json` 已被 Git 忽略。

### 4.2 PostgreSQL 后端

PostgreSQL 模式使用以下表：

| 表 | 关键字段 | 用途 |
| --- | --- | --- |
| `user_preferences` | `user_id`, `pref_type`, `pref_value JSONB` | 每类偏好一行，主键为用户和偏好类型 |
| `chat_history` | `user_id`, `session_id`, `role`, `content`, `created_at` | 完整聊天记录 |
| `trip_history` | `trip_id`, `user_id`, 起终点、日期、目的 | 历史行程 |
| `user_statistics` | 消息数、行程数、查询数、高频目的地 JSONB | 用户统计 |
| `active_trip_contexts` | `user_id`, `status`, `context_data JSONB` | 每位用户唯一的当前出差任务 |

`PostgresLongTermMemory` 初始化时会幂等创建这些表，并确保当前用户有一条统计记录。连接使用 `autocommit=True`，目前每个 `MemoryManager` 持有一条长期连接，没有连接池和显式关闭逻辑。

应用启动迁移还会创建 `active_trip_contexts`，使当前任务表在业务实例初始化前也可存在；其他核心记忆表目前仍由 `PostgresLongTermMemory._init_schema()` 创建。

### 4.3 disabled 后端

`disabled` 使用内存中的默认空结构，但所有 `_save()` 都是空操作。它适合临时关闭长期持久化，不是故障时的自动降级方案。

## 5. 什么内容会被记住

### 5.1 每轮聊天

`MemoryManager.add_message()` 会同时写短期和长期记忆：

- 用户消息：意图识别完成后、Agent 编排前写入；
- 助手消息：Agent 编排成功并格式化结果后写入；
- 简单闲聊：用户和助手文本都会直接写入。

普通业务请求的长期助手消息目前保存的是序列化后的编排结果 JSON，不只是前端显示的最终文本。若编排失败，用户消息可能已经写入，而对应助手消息不会写入。

### 5.2 用户偏好

偏好有两条写入路径：

- 首次引导直接保存 `home_location`、`transportation_preference`、`hotel_brands`、`seat_preference`；
- `preference` Agent 返回偏好后，由协调器按 `append` 或 `replace` 规则回写。

偏好存储支持字符串、列表等 JSON 值。`hotel_brands` 等列表型偏好会去重追加。

### 5.3 当前出差任务

`event_collection` Agent 提取到起点、目的地、日期或工作地点后，协调器调用 `update_active_trip()` 增量合并：

- 新值覆盖同名旧值；
- `None` 不覆盖已有值；
- 每位用户只保留一个当前任务；
- PostgreSQL 使用 `context_data JSONB`，文件模式使用顶层 `active_trip`。

当前任务会在下一轮作为 system context 注入，因此“补贴呢”“返程改周五”等省略表达仍可关联到正在进行的出差。

### 5.4 历史行程与统计

当 `itinerary_planning` 成功产生可用行程，并且事项收集结果中存在目的地时，协调器写入一条历史行程，同时更新：

- `total_trips`；
- 对应目的地的访问次数；
- 高频目的地排名。

`total_messages` 随长期聊天消息增加。`total_queries` 当前只在 CLI 查询流程中增加，Web 流程没有调用该计数。

## 6. 记忆如何进入 Agent 上下文

### 6.1 意图识别

Web 在复杂请求的意图识别前组装：

1. 当前出差任务；
2. 长期记忆摘要；
3. 最近 5 轮短期对话；
4. 本轮用户输入。

长期摘要由以下内容组成：

- 全部非空用户偏好；
- 非当前会话聊天的 LLM 摘要，最多读取 20 条；
- 当前出差任务；
- 最多 3 条历史行程，优先选择与本轮地点匹配的行程。

摘要生成失败时返回空字符串；如果已经存在缓存，则继续使用旧缓存。Web 和 CLI 都尝试每增加 5 条短期消息后刷新摘要，避免每轮都调用 LLM。

### 6.2 子 Agent 编排

协调器向子 Agent 提供结构化上下文：

```json
{
  "recent_dialogue": [{"role": "user", "content": "最近 3 轮对话中的一条消息"}],
  "user_preferences": {"hotel_brands": ["全季"]},
  "active_trip": {"destination": "上海", "status": "active"},
  "rewritten_query": "改写后的本轮问题"
}
```

不同 Skill 可以按需读取这些字段。例如事项收集会读取当前任务继续补齐信息，合规检查会结合当前任务和前序规划结果。

### 6.3 主动查询记忆

用户询问“我去过哪些地方”“我之前说过什么偏好”时，意图会路由到 `memory_query`。该 Agent 最多读取：

- 50 条历史行程；
- 全部用户偏好；
- 最近 5 轮短期对话；
- 最多 30 条历史聊天生成的摘要。

最终回答由 LLM 基于这些来源生成；没有记录时，Prompt 要求明确说明没有相关信息，不能编造。

## 7. 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOMMEY_SHORT_TERM_BACKEND` | `memory` | `memory` 或 `redis` |
| `HOMMEY_SHORT_TERM_MAX_TURNS` | `10` | 短期滑动窗口轮数 |
| `HOMMEY_REDIS_HOST` | `127.0.0.1` | Redis 地址 |
| `HOMMEY_REDIS_PORT` | `6379` | Redis 端口 |
| `HOMMEY_REDIS_DB` | `0` | Redis DB |
| `HOMMEY_REDIS_PASSWORD` | 空 | Redis 密码 |
| `HOMMEY_REDIS_KEY_PREFIX` | `hommey:short_term` | 短期 key 前缀 |
| `HOMMEY_LONG_TERM_BACKEND` | `file` | `file`、`postgres` 或 `disabled` |
| `HOMMEY_MEMORY_STORAGE_PATH` | `data/memory` | 文件后端路径 |
| `HOMMEY_POSTGRES_DSN` | 空 | PostgreSQL 连接串 |

Docker Compose 默认把短期后端设为 Redis、长期后端设为 PostgreSQL。非 Docker 本地运行若不设置环境变量，则使用进程内短期记忆和 JSON 文件长期记忆。

后端不可用时没有自动切换：Redis 或 PostgreSQL 初始化/读写失败会让相应请求失败。只有长期摘要的 LLM 调用采用“失败返回空摘要或旧缓存”的软降级。

## 8. 清理与保留

| 操作 | 短期对话 | 长期聊天 | 偏好 | 历史行程 | 当前任务 |
| --- | --- | --- | --- | --- | --- |
| `short_term.clear()` / `end_session()` | 清除 | 保留 | 保留 | 保留 | 保留 |
| `long_term.clear_history()` | 不处理 | 清除 | 保留 | 清除 | 保留 |
| `clear_active_trip()` | 不处理 | 保留 | 保留 | 保留 | 清除 |
| 文件后端 `delete_all()` | 不处理 | 清除 | 清除 | 清除 | 清除 |

当前 Web API 没有暴露“结束会话”“删除全部记忆”或数据导出接口。PostgreSQL 实现也没有与文件后端对应的 `delete_all()`。长期聊天和行程没有自动保留期限。

## 9. 隐私与安全

- Web API 通过 JWT 身份与路径用户 ID 一致性检查防止横向读取。
- PostgreSQL 查询使用参数化 SQL。
- 本地记忆 JSON 不进入 Git。
- 密码不会进入记忆表；认证用户表与记忆表只通过逻辑上的 `user_id` 对应，目前没有外键。
- 记忆内容没有应用层加密，安全性依赖磁盘、Redis 和 PostgreSQL 的部署权限及传输配置。
- 长期聊天保存用户原文和编排结果，可能包含个人偏好、地点、日期等敏感信息。
- 当前日志会记录用户问题、部分回答和部分偏好值；生产环境应结合日志访问控制和保留期限管理。

## 10. 当前实现边界

以下是维护时需要特别注意的现状：

1. Redis 短期 key 没有 TTL，旧 session key 需要额外清理。
2. Web 没有显式退出会话或删除记忆的后端接口。
3. PostgreSQL 长期连接按用户实例持有，没有连接池和显式关闭。
4. PostgreSQL 聊天和行程查询目前没有专门的 `(user_id, created_at)` 索引，大数据量下需要补充。
5. Web 的摘要刷新计数直接读取内存列表长度；Redis 后端下该列表不增长，因此摘要通常只在首次生成，后续不会按预期每 5 条刷新。CLI 使用后端统计，不受此问题影响。
6. 长期聊天摘要排除当前 `session_id`；当前会话中超出短期窗口的旧消息不会进入历史摘要，直到创建新会话。
7. 文件后端和 PostgreSQL 后端的全量删除能力不一致。
8. 记忆表除当前任务外主要由运行时代码建表，尚未全部纳入版本化迁移。

## 11. 检查与测试

查看 Redis 短期 key：

```bash
docker exec hommey-redis redis-cli --scan --pattern 'hommey:short_term:*'
```

查看 PostgreSQL 各类记忆数量：

```sql
SELECT user_id, count(*) FROM chat_history GROUP BY user_id;
SELECT user_id, count(*) FROM trip_history GROUP BY user_id;
SELECT user_id, count(*) FROM user_preferences GROUP BY user_id;
SELECT user_id, status, updated_at FROM active_trip_contexts;
```

相关测试：

```bash
PYTHONPATH=. pytest -q \
  tests/test_long_term_memory_postgres.py \
  tests/test_skill_platform.py
```

`tests/test_memory_system.py` 是需要真实 LLM 的集成脚本，默认跳过；设置 `HOMMEY_RUN_LLM_TESTS=1` 后才会执行。
