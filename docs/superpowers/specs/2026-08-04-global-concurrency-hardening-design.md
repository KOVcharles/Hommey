# Hommey 全局并发加固与水平扩展 — 设计文档

日期：2026-08-04
状态：已批准（用户确认设计，含三条反馈修订）

## 1. 背景与问题

Hommey 是面向企业差旅的智能 Agent（FastAPI + AgentScope 多智能体编排）。当前部署为单 uvicorn worker、单进程。经审计，全局/Web 层的并发管理存在明显缺陷：

- **无全局并发上限**：多个用户同时发消息时，每个请求最多 8 次 Agent 调用会同时打向 LLM。`ExecutionBudget` 只限制单请求内部调用次数，不限制系统同时处理多少请求。
- **同用户请求无串行化**：`HommeyWebInstance` 是每用户进程内单例，`_summary_cache`、`_total_messages`、`session_id`、`memory_manager` 无锁共享。`/chat` 与 `/chat/stream` 指向同一实例，同一用户并发消息会互相覆盖记忆、摘要缓存竞态。
- **用户实例注册表 check-then-act**：`get_or_create` + `initialize_user` 对同一新用户并发请求可能重复初始化同一实例。
- **无法水平扩展**：`WebHommeyManager._instances` 是进程内字典，多 worker 下用户实例分家，状态不同步。
- **熔断器是每 worker 每用户进程内状态**：多 worker 下失败分散，全局熔断失效（N 个 worker 各失败 N 次才触发）。

任务编排层内部（并行度、依赖分批、执行预算、超时、重试、熔断）已有较好管理，本次不改动编排逻辑，只加固服务入口层。

## 2. 目标与非目标

### 目标

1. 同用户请求跨 worker 串行，保证会话上下文一致。
2. 全局并发上限，防止 LLM 调用雪崩。
3. 熔断器全局化（所有 worker 共享同一状态）。
4. 解除同步 SQL 阻塞事件循环的隐患。
5. 支持单容器多 worker 水平扩展（复用现有 Redis/Postgres）。
6. 不破坏现有接口（`CircuitBreaker`、`HommeyWebInstance.process_message`），保持现有测试兼容。

### 非目标（YAGNI）

- 无状态 worker（每次请求重建 runtime）—— 小规模下每 worker 实例缓存收益更大。
- 多容器 + 负载均衡 —— 单机价值有限。
- 记忆层整体重写 —— 只在 API 层加 async 门面。
- 队列公平性、信号量优先级。

## 3. 架构总览

```text
                 ┌──────────────────────────────────────────────┐
   HTTP/SSE      │  WebHommeyManager.process_message (统一入口)  │
 ──────────────► │  1. per-user asyncio.Lock   (进程内, 减争用)  │
                 │  2. DistributedLock per-user (Redis, 跨worker)│
                 │  3. RedisSemaphore 全局     (Redis, 并发上限) │
                 │  4. 心跳续约任务 (持锁期间)                    │
                 │  5. 逆序释放 / 超时→UpstreamError             │
                 └──────────────────────────────────────────────┘
                       │              │              │
                 Redis 协调层     记忆层 async 门面  编排层(不动)
              (Lua 原子原语)    (to_thread 线程池)
                 │  │  │              │
              Lock│Sem│熔断      Postgres/Redis 落库
```

多 uvicorn worker 共享同一 Redis/Postgres。跨 worker 一致性与并发控制全部依赖 Redis 协调层；数据落库一致性依赖 Postgres 已有的 `pg_advisory_xact_lock`。

## 4. 部署层

### 4.1 多 worker

- Dockerfile CMD 改为：`uvicorn webui_new.server:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-2}`。
- compose `environment` 注入 `UVICORN_WORKERS`（默认 2，小规模 <50 在线请求足够）。
- 每 worker 各自持有 Postgres 连接池（psycopg pool 本就是 per-process），无需额外处理。
- dev 模式（volume 挂载，无 `--reload`）与多 worker 兼容：改代码后 `restart hommey` 即可。

### 4.2 迁移并发

`webui_new/auth/migrations.py::apply_all_migrations` 当前先查 `schema_migrations` 再执行迁移，多 worker 同时启动会双双通过检查、执行重复迁移，`INSERT` 主键冲突。

修复：整个迁移应用过程用 `pg_advisory_xact_lock` 包裹（如 `SELECT pg_advisory_xact_lock(hashtext('hommey_migrations'))`），迁移只执行一次。

## 5. Redis 协调层（新增 `utils/redis_coordination.py`）

进程共享一个 `redis.asyncio` 客户端（复用 `MEMORY_CONFIG.short_term` 的 host/port/password/db）。**所有原语用 Lua 原子脚本实现**，不使用多命令组合（避免非原子窗口）。

### 5.1 `DistributedLock`（跨 worker 同用户串行）

- **获取**（Lua）：`if SET key token NX PX(ttl) then return 1 else return 0 end`。token 为全局唯一 UUID4。
- **续约心跳**（Lua）：`if GET key == token then PEXPIRE key ttl return 1 else return 0 end`。持锁期间每 `ttl/3` 秒续一次；续约失败表示锁已易主，持有方须中止操作（防止旧协程误删新协程的锁）。
- **释放**（Lua）：`if GET key == token then DEL key return 1 else return 0 end`。token 校验防止误删他人持有的锁。
- 拿不到锁 → 按小间隔 sleep 重试；等待超过 `PER_USER_LOCK_TIMEOUT_SEC` → 抛 `UpstreamError`（用户排队超时）。

### 5.2 `RedisSemaphore`（全局并发上限）

- **获取**（Lua）：`local n = INCR key; if n <= max then EXPIRE key ttl return 1 else DECR key return 0 end`。原子地完成检查-增加-设过期，崩溃只在"设过期前崩溃"的窗口内留下一个计数，靠 TTL 兜底回收。
- **释放**（Lua）：`DECR key`；结果 <0 时 clamp 回 0（防御性）。
- 获取超时（`SEMAPHORE_ACQUIRE_TIMEOUT_SEC`）→ 抛 `UpstreamError`（服务繁忙）。

### 5.3 `RedisCircuitBreaker`（全局熔断）

- 状态 + 失败计数存 Redis（带 TTL，防 worker 崩溃残留）。
- Lua 原子执行：
  - `record_failure`：计数 +1，达 `failure_threshold` 置 OPEN 并记录 opened_at。
  - `record_success`：CLOSED 下清零；HALF_OPEN 下计数 +1，达 `half_open_successes` 置 CLOSED。
  - `state` 读：OPEN 且超过 `recovery_timeout_sec` → 转 HALF_OPEN。
- **保持原 `CircuitBreaker` 接口**：`raise_if_open()` / `record_failure()` / `record_success()` / `get_status()`。`runtime.py::create_circuit_breaker()` 返回 Redis 版，`HommeyWebInstance` 调用点零改动。

## 6. Web 层改造

### 6.1 统一消息入口

`WebHommeyManager` 新增 `async def process_message(user_id, message, ...)` 作为普通 `/chat` 与 SSE `/chat/stream` 共用入口。**固定顺序取锁**：

1. 进程内 per-user `asyncio.Lock`（减少 Redis 争用；同一 worker 内同一用户不重复进 Redis）。
2. `DistributedLock` per-user（跨 worker 串行）。
3. `RedisSemaphore` 全局（并发上限）。
4. 持锁期间启动心跳续约任务（每 `LOCK_HEARTBEAT_INTERVAL_SEC` 续一次）。

处理完**逆序释放**（先 semaphore，再 distributed lock，最后进程内锁）。任一锁等待超时 → `UpstreamError`。

`HommeyWebInstance.process_message` 内部逻辑不动，由 manager 入口包装调用。

### 6.2 SSE 流式与取消

- `chat.py` 的 `/chat/stream` 路由改为调用 manager 入口。
- SSE 生成协程在 `finally` 中释放锁，并捕获 `asyncio.CancelledError`（前端断连）→ 立即释放锁，不等到 TTL 过期。现有 [chat.py:87](webui_new/routes/chat.py#L87) 已捕获 `CancelledError`，需将锁释放放入该路径。
- 心跳续约任务在协程退出时取消（`task.cancel()` + await），避免孤儿任务。

### 6.3 实例初始化竞态

`initialize_user` 的 check-then-act 用进程内 per-user `asyncio.Lock` 保护，防止同一新用户并发请求重复初始化。

## 7. 记忆层 async 门面（新增 `context/async_memory.py`）

不重写记忆层。暴露薄 async 门面，所有 I/O 走 `asyncio.to_thread` 提交到线程池，**同步 API 保持不变**（大量同步调用方不破坏）。

覆盖方法：

- `async def add_message(role, content, metadata)` → `to_thread(sync_add_message)`
- `async def get_preference()` / `save_preference(...)` → `to_thread`
- `async def get_active_trip()` / `update_active_trip(...)` / `complete_active_trip(...)` / `cancel_active_trip(...)` → `to_thread`
- `async def get_recent_context(n)` → `to_thread`
- `async def save_trip_history(...)` / `get_trip_history()` → `to_thread`

`HommeyWebInstance` 改调 async 门面。

**顺序保证说明**：`asyncio.to_thread` 的执行顺序不保证，但同用户串行锁已保证请求级顺序；不同用户的 to_thread 在线程池内并发落库（线程池有上限兜底）。事件循环不再被同步 SQL 阻塞。

**为什么不用 asyncpg**：psycopg3 pool 是同步阻塞的，整体换 asyncpg 是更大的改动且会重写 repository 层。`to_thread` 是本次范围（小规模 <50）下成本最低、侵入最小的解。若规模增大，可在后续单独迁移。

## 8. 配置（`settings.py` 新增 `CONCURRENCY_CONFIG`）

| 项 | env | 默认 | 说明 |
|---|---|---|---|
| 全局并发上限 | `HOMMEY_GLOBAL_CONCURRENCY_LIMIT` | 8 | RedisSemaphore 上限 |
| 同用户排队超时 | `HOMMEY_PER_USER_LOCK_TIMEOUT_SEC` | 60 | DistributedLock 等待超时 |
| 信号量获取超时 | `HOMMEY_SEMAPHORE_ACQUIRE_TIMEOUT_SEC` | 120 | RedisSemaphore 获取超时 |
| 锁 TTL（基准） | `HOMMEY_DISTRIBUTED_LOCK_TTL_SEC` | 45 | 每次续约重设的 TTL |
| 心跳间隔 | `HOMMEY_LOCK_HEARTBEAT_INTERVAL_SEC` | 15 | 持锁续约周期 |
| 锁等待重试间隔 | `HOMMEY_LOCK_RETRY_INTERVAL_SEC` | 0.2 | 拿锁重试 sleep 间隔 |
| worker 数 | `UVICORN_WORKERS` | 2 | compose 注入，非 settings |

**锁超时关系（关键，吸收评审反馈）**：

- 锁 TTL（45s）≈ 心跳间隔 × 3（45s）：持锁协程每 15s 续约，TTL 足以覆盖 3 个周期内的心跳延迟。
- 锁等待超时（60s）> 心跳周期（15s）：即使持锁协程崩溃，锁在 ≤45s 内被 TTL 清掉，新请求最多等 45s 即获锁 —— 消除"旧协程持锁、新协程干等 240s"的死锁窗口。
- 续约失败即中止：token 校验 + 每 15s 一次检查，旧协程不可能在锁易主后误删新协程的锁。

Redis 连接复用 `MEMORY_CONFIG.short_term` 的 host/port/password/db。

## 9. 测试

### 9.1 单元测试

- Lua 脚本原子性：信号量获取/释放、锁获取/续约/释放脚本逻辑。
- `DistributedLock`：获取、续约、释放、防误删（token 不符不删）、等待超时抛错。
- `RedisSemaphore`：上限内放行、超上限等待、释放后恢复、TTL 泄漏回收。
- `RedisCircuitBreaker`：状态机 CLOSED→OPEN→HALF_OPEN→CLOSED 完整迁移。

### 9.2 集成测试

- 同用户两个并发请求顺序完成（进程内锁 + DistributedLock 生效）。
- 多 worker 并发启动迁移只执行一次（advisory lock 生效）。
- SSE 断连（`CancelledError`）立即释放锁。

### 9.3 兼容性

`CircuitBreaker` / `HommeyWebInstance.process_message` 接口不变，现有测试不受影响。

## 10. 风险与权衡

| 风险 | 缓解 |
|---|---|
| Redis 协调层成为单点 | 复用现有 hommey-redis；锁/信号量/熔断 TTL 兜底；Redis 不可用时熔断降级（复用现有 CircuitOpenError 路径） |
| `to_thread` 线程池放大并发 | 线程池有默认上限；同用户串行锁已限制单用户落库并发 |
| Lua 脚本复杂度 | 脚本极短（<15 行），全部收敛在 `redis_coordination.py`，单元测试覆盖 |
| 心跳任务生命周期 | SSE/请求 `finally` 取消心跳任务 + 释放锁，避免孤儿任务 |

## 11. 明确不做

- 无状态化、多容器 + LB
- 短期记忆/落库整体 asyncpg 迁移
- 队列公平性、信号量优先级
- 编排层内部改造
