# 同一用户多窗口互相阻塞

| | |
| --- | --- |
| 编号 | BUG-2026-09-15-01 |
| 发现日期 | 2026-09-15 |
| 状态 | 代码已修复；隔离 PostgreSQL/Redis 与双标签页回归通过；业务容器尚未重启验收 |
| 影响面 | 同一用户只要有任意一个任务在进行中，该用户的**所有**会话操作都被阻塞 |

## 修复记录（2026-09-15）

以下一至七节及附录保留修复前的调查记录；当前实现与验收结果以本节为准。

### 已实现的行为

- 同一用户的不同会话可以同时处理普通聊天和流式聊天；读取历史、切换历史、新建会话不再等待其他会话生成结束。
- 同一会话的并发生成或删除、重命名立即返回 `SESSION_BUSY`（HTTP 409；流式接口返回错误事件），不排队 60 秒、不自动重试。
- 清空全部历史采用独占维护租约；有任一会话正在写入时返回 `HISTORY_BUSY`。维护期间也禁止新建会话及新的写入，避免删除与生成互相穿插。
- 前端加载历史失败时显示错误和重试入口，不再显示“没有历史”；保留已有列表及当前标签页选择。
- 当前行程查询显式传入 `session_id`，切换、新建、删除后刷新面板；丢弃已切走会话的迟到响应。

### 实现与新增发现

1. `HommeyWebInstance` 移除“用户当前会话”。`MemoryManager.for_session` / `MemoryService.for_session` 创建每次请求独立的绑定视图，隔离 session、缓存 facade、`current_request_id` 和 `_current_turn_id`；底层连接池与用户偏好存储继续复用。无需复制模型或为每个会话永久缓存运行时。
2. 工厂默认构建未绑定会话的记忆服务；显式传入的构造参数现在校验并绑定指定会话，不再忽略。聊天绑定校验用户归属及删除状态；旧的 switched/idle 会话可恢复，deleted/cleared 会话不可复活。
3. 进程内会话锁使用弱引用避免积累；Redis `SessionActivityLock` 原子协调会话互斥和用户级历史维护，保留心跳、失锁取消、Redis 不可用时拒绝写入及全局生成配额。只读接口不取生成锁。
4. 数据库还有一个报告初始勘查遗漏的限制：`uq_conversation_sessions_active_user` 唯一索引。新增迁移 `0024_concurrent_sessions.sql` 移除此索引；新建会话不再关闭同用户其他会话。
5. `BusinessServices.context(scope)` 和活动行程检索均使用请求会话。跨会话重复使用请求 ID 会被拒绝，避免幂等重试读写另一会话的消息。
6. 取消请求时，已提交到线程池的消息写入等待实际完成后才释放会话锁；不同会话的取消互不影响。

### 验收

使用独立的 `docker/docker-compose.test.yml`（PostgreSQL 55432、Redis 56379），未操作业务库。

- 相关 Python 测试：**153 passed，1 skipped**。唯一跳过项为原有 `test_facade_wraps_memory_manager` 占位检查，不是数据库集成测试。
- `tests/test_concurrent_sessions_integration.py`：真实 PostgreSQL + Redis；同一管理器及两个独立管理器模拟 worker，共享用户下的消息、turn、request、上下文、行程隔离；普通与流式请求并行；读历史/新建不阻塞；同会话冲突；取消隔离；维护互斥及删除后不能恢复。
- `tests/test_redis_coordination.py`：真实 Redis 租约续期、过期、错误 token、不同 TTL 的并发会话及维护互斥。
- `scripts/check_session_concurrency_ui.py`：Microsoft Edge 无头双标签页测试；A 的流保持挂起时，B 查看历史、新建、发送；断言两个请求使用不同 session；历史失败/重试、刷新保留选择及会话行程查询；无 JS 异常。此浏览器测试使用 API 桩，不调用模型。
- `.repro/session_scope.py`：原有刷新/切换/新标签页边界回归通过。
- `node --check webui_new/static/app.js` 与 `git diff --check` 通过。

### 启用与验证边界

- 服务启动时会执行迁移 `0024`，需要所有 API worker 一起加载新代码，并刷新前端。开发容器挂载源码时，重启 hommey 服务即可；镜像部署需要重新构建。
- **不要混跑新旧 worker**：旧版本的用户锁与新版本的会话锁不是同一套互斥范围。应先排空/停止旧 worker，再启动全部新 worker。
- 尚未重启业务容器，也未用真实模型执行两条完整差旅任务；当前验证覆盖真实协调/存储和浏览器行为，模型阶段用可控等待替身模拟。
- 同一页面仍只维护一个正在生成的界面；本次交付的是多个窗口/标签页独立会话并发。所有会话仍受既有全局并发配额约束。

## 一、用户现象

同一个用户开两个窗口。窗口 A 发起一次差旅规划（流式回答进行中），此时窗口 B：

- 打开侧边栏，**历史会话列表是空的**，显示"还没有历史会话"
- 点击「新建会话」没有效果
- 刷新页面同样看不到历史

用户原话："我一个窗口在进行中，另外一个窗口就完全不能操作了，不能开新的窗口。"

## 二、执行证据（代码定位）

链路自下而上：

1. **锁的粒度是用户，不是会话**
   `webui_new/manager.py:639` 维护 `self._user_locks: dict[str, asyncio.Lock]`，
   `webui_new/manager.py:712` `_user_lock_scope(user_id)` 在此之上再叠加 Redis 分布式锁与全局信号量。

2. **聊天流从头持有该锁直到整轮结束**
   `webui_new/manager.py:935` `stream_message` 内部于 `manager.py:961` 进入 `_user_lock_scope`。
   一次差旅规划的整轮耗时以十秒计。

3. **会话接口全部抢同一把锁，包括纯读的**
   `webui_new/routes/chat.py` 有 7 处 `run_user_state_operation`（164 / 174 / 191 / 205 / 222 / 235 / 245），
   其中 `GET /sessions`（164，列会话）与 `GET /sessions/{id}`（191，取消息）
   **只读数据库，却同样要抢用户的排他锁**。

4. **等待代价是 60 秒**
   `webui_new/manager.py:725` 取 `per_user_lock_timeout_sec`（默认 60.0），
   本地锁获取也纳入同一 deadline；超时抛
   `UpstreamError("USER_QUEUE_TIMEOUT", "您有请求正在处理，请稍候再试。")`（`manager.py:739`）。

5. **前端把失败呈现为「没有历史」**
   `webui_new/static/app.js` 的 `loadSessions()` 在 catch 分支执行 `renderSessions([])`，
   于是被阻塞 60 秒后的失败，在界面上表现为一个空列表。

**未受影响的路径**（用于界定范围）：`/status`、`/init`、`/summary`、`/trip/active`
走的是 `manager.get_initialized_user()`，只经过 `_per_user_lock`（仅锁初始化那一瞬），
不进入 `_user_lock_scope`。所以窗口 B 能打开页面，只是登录后的一切会话操作都不行。

## 三、根因

**不在锁本身，而在有一个共享的 per-user 可变状态逼着锁只能加在用户上。**

`webui_new/manager.py:51`：

```python
self.session_id = str(uuid.uuid4())[:8]
```

`HommeyWebInstance` 持有 `session_id`，语义是"这个用户当前停在哪个会话"。由此产生两个后果：

- 任何读写它的操作都可能与别的窗口互相干扰 → 必须串行 → 锁粒度只能是 user；
- 实例是**按 user_id 永久缓存**的（`manager.py:648` 的 `_instances`，无 TTL、无驱逐），
  这个状态在整个进程生命周期内是全局共享的。

而这个状态还向下绑定了一整条链：

```
HommeyWebInstance.session_id                        ← 请求侧
  │
  └─ activate_chat_session(session_id)              manager.py:890-895 / :970-975
       │    （每个聊天请求进入用户锁后都会执行一次）
       │
       └─ MemoryManager.session_id                  context/memory_manager.py:54-58
            └─ MemoryService.session_id             context/memory_service.py
                 └─ append_message(user_id, session_id=self.session_id, ...)
                      memory_repository.py:144-146 ← 存储侧，只认这个值
```

**两点需要特别澄清**（初判时我理解错了）：

1. **`create_agent_runtime(session_id=...)` 在 Postgres 后端上是惰性的。**
   `runtime.py:73-76` 把它传给 `MemoryManager`，但 `MemoryService.__init__:85-86`
   在 Postgres 分支调用 `get_or_create_session(user_id, idle_timeout)` 时**丢弃了 `requested_session_id`**。
   所以"运行时的会话"完全由后来的 `activate_chat_session` 决定，不由工厂参数决定。

2. **聊天路径其实已经是请求驱动的。**
   `POST /chat` 与 `POST /chat/stream` 从 `ChatRequest.session_id` 取值
   （`schemas/requests.py:77`），进入 `process_message` / `stream_message` 后
   调 `activate_chat_session(session_id, allow_empty=True)`。
   也就是说"请求里的会话优先"**已经存在**——只是它发生在**用户锁之内**。

因此真正缺的不是"把 session 传进来"，而是：
- 这把锁必须在**会话**上，而不是**用户**上（否则两个窗口只能排队）；
- 任何**不经过聊天回合**却要读会话的接口（`GET /trip/active`、`GET /sessions`）
  目前只能读实例状态，无法从请求获得会话。

`MemoryManager` 是实例级、长期存活的，且它的操作**不接受 session 参数**——
`add_message(self, role, content, metadata=None)`（`context/memory_manager.py:62`）
内部一律使用 `self.session_id`。不过底层已经参数化了：
`memory_repository.append_message(user_id, session_id=..., ...)` 接受显式 session，
所以中间层是"写死了 `self.session_id`"，而不是"架构上不支持"。

## 四、参考做法：对话式产品怎么划分并发边界

ChatGPT / Claude 这类产品的并发边界在**对话**上，不在**用户**上：

- 多个标签页开不同对话，可以同时各自生成，互不阻塞；
- 同一个对话内不允许并发生成——正在生成时输入框变为「停止生成」，要先停或等结束。

它能这样做的前提是：**服务端不保存"用户当前会话"这种状态**。
请求自带对话标识（`POST /conversation/{conversation_id}/messages`），
服务端读请求即知该写哪里，没有共享可变状态，
因此可以并行处理不同对话，只需对同一个 `conversation_id` 加锁。

对照本项目：`session_id` 被放在实例上，等于人为制造了一个"用户当前会话"的全局状态，
使锁粒度无法下探到会话。

## 五、修复方向与改动面

核心是**把 `session_id` 从实例状态降级为请求参数**，与此配套：

1. `HommeyWebInstance` 不再持有 `session_id`；`Scope(user_id, session_id)` 的
   session_id 改从请求取（前端**已经在传** `session_id: activeSessionId`）。
2. 锁粒度由 `user` 改为 `user + session`。
3. 记忆写入改为按调用传入 session，而不是构造时固定——需要把 `MemoryManager`
   的一系列方法参数化（`add_message` / `get_recent_context` / `get_active_trip` 等）。
4. `get_active_trip` 这类"当前会话"查询改为显式传 session。

> 依赖点全量清单见文末附录。

**改动量评估：中等规模的机械重构，比初判乐观。**

有利条件（勘查后确认）：

- 聊天路径**已经是请求驱动的**——`ChatRequest.session_id` 一路传到 `activate_chat_session`，
  要做的只是把 `activate` 去掉、改成把 id 直接喂给 `Scope` 与记忆层；
- 存储层**已经参数化**——`memory_repository.append_message(user_id, session_id=..., ...)`
  本来就接受显式 session，是中间层写死了 `self.session_id`；
- `runtime.py:59` 的 `session_id` 在 Postgres 上是惰性的，可以自由重新解释或删除；
- `GET /sessions` 返回的 `active_session_id` 是**死代码**——前端从不读它
  （`app.js` 只读 `data.sessions`，`activeSessionId` 完全由客户端持有），可以直接删字段；
- `manager.py:153` 的 `"active"` 标志同样无人消费，侧边栏高亮用的是客户端 id。

主要工作集中在两处：**中间层会话参数化**（`MemoryService` → `MemoryManager` → `AsyncMemoryFacade`）
与**锁粒度下沉**。风险集中在附录里的「隐藏耦合」。

### 分阶段建议

- **第一阶段（低风险，可独立交付）**：把纯读接口移出 `_user_lock_scope`。
  `GET /sessions` 可直接删掉 `active_session_id` 字段；`GET /sessions/{id}`、
  `GET /sessions/{id}/execution-plans` 本就用路径参数。可让窗口 B 恢复「能看」。
- **第二阶段**：缩短锁等待（当前 60 秒），让写操作快速失败并给出明确提示，
  而不是让用户干等。
- **第三阶段**：完成会话参数化 + 锁粒度由 user 降为 user+session，
  使不同会话真正并行。

## 六、验证状态

**本轮未做任何实测。** 定位完全来自代码阅读。

原因：本地容器全部停止——

```
$ docker ps
（无输出）
$ docker ps -a --filter name=postgres
hommey-postgres | Exited (0) 21 hours ago
```

因此 `check_journey_flow_ui.py`、`check_trip_choices_ui.py` 等依赖真实服务的检查无法运行，
memory 集成测试也因连不上 PostgreSQL 被跳过。修复前需先恢复服务。

## 七、尚未消除的风险

- **未实测**：以上链路均为静态分析结论，未经运行时验证。
- **`USER_QUEUE_TIMEOUT` 的重试语义**：该错误标记 `retryable=True`，
  需确认前端不会对其自动重试而放大阻塞。
- **分布式锁的 fail closed 行为**：`_user_lock_scope` 在 Redis 不可用时拒绝放行
  （`manager.py:749` 起），改造时需保留该语义。
- **跨 worker 场景**：进程内锁之外还有 Redis 分布式锁，
  第二阶段的超时调整需同时覆盖两条路径。

## 附录：依赖点全量清单

### 已具备请求 session、改动机械

| 位置 | 说明 |
| --- | --- |
| `routes/chat.py:41-80` / `:82-150` | `POST /chat` / `/chat/stream`，已从 `ChatRequest.session_id` 取值 |
| `manager.py:890-895` / `:970-975` | 两者已在锁内调 `activate_chat_session(session_id, allow_empty=True)`，改造即删除此调用 |
| `routes/chat.py:152-160` | `POST /orchestration/interrupt`，已有 `InterruptRequest.session_id` |
| `manager.py:336` / `:468` | 两处 `Scope(...)` 构造，`session_id` 就在旁边 |
| `routes/chat.py:185-248` | `GET/PATCH/DELETE /sessions/{id}`、`execution-plans` 已用路径参数 |
| `runtime.py:59-76` | 工厂的 `session_id` 在 Postgres 上惰性，可删或重解释 |

### 需要补传参数

| 位置 | 说明 |
| --- | --- |
| `context/memory_service.py` | `append_message` / `get_recent_context` / `get_statistics` / `close_session` 改为接受 session 参数 |
| `context/memory_manager.py:62-88` | `add_message` 同上；`:123-151` 的 `get/update/complete/cancel_active_trip` 目前无参 |
| `context/async_memory.py:14-49` | **没有绑定接缝**——构造只收 `memory_manager`，方法全部动态读 `self._m.session_id` |
| `webui_new/manager.py:326` | `get_active_trip()` 无 session 参数 |
| `routes/users.py:70-73` | `GET /trip/active` **请求里根本没有 session 字段**，需加查询参数并让 `app.js:1277` 传 |
| `manager.py:187` | `_validate_intake_submission` 目前隐式依赖同锁内先执行过 `activate_chat_session`，改造后须改为显式接收 id |

### 隐藏耦合（风险所在）

1. **`agent_runtime/services.py:104`** —— `context(scope)` 手里就有 `scope.session_id`，
   却去读 `self.memory.get_active_trip()`（管理器上的可变会话）。
   **为会话 A 组装上下文时，可能拿到会话 B 的行程。**
2. **`agent_runtime/services.py:81-83`** —— 记忆检索按 `user_id` 单独过滤
   `active_trip_contexts`，**所有会话的行程都会漏进检索结果**。
   （第 1、2 条是独立于本 bug 的数据泄漏问题。）
3. **`memory_service.py:85-86`** —— Postgres 分支丢弃 `requested_session_id`，
   任何"以为工厂参数就建立了会话绑定"的改动都是错的。
4. **`manager.py:187`** —— 依赖执行顺序，重排会破坏行程卡片重提交校验，
   并给出误导性的"卡片已归档"错误（`manager.py:194`、`:201`）。
5. **`memory_service.py:245-263`** `activate_session` —— 会清 Redis 缓存、
   做 `SELECT ... FOR UPDATE` 并关闭上一个活动会话行（`memory_repository.py:212-220`）。
   按请求执行它是当前的成本来源；同时注意 `memory_repository.py:206-210`
   在会话行不存在时抛 `Session not found`，所以"参数化 session"仍要求该行已存在。
6. **`AsyncMemoryFacade` 无绑定接缝** —— 要么给每个方法加 session 参数，
   要么改为按请求构造一个 facade。
7. **`long_term_memory.py:262-263`、`:280-282`** —— `session_id=None` 会**静默回退**到
   全局 `data["active_trip"]`。任何"忘了传 id"的调用点不会报错，而是退化为跨会话污染。
8. **`.repro/session_scope.py`**（本轮为会话边界写的 Playwright 回归）——
   其路由桩仍返回 `active_session_id`，是改造时需要同步更新的护栏。
