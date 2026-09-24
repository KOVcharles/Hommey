# 页内并行会话设计

## 背景

用户反馈：对话进行中无法新建会话。期望是 ChatGPT 那样的体验——一个会话在跑的时候，
可以切到别的会话、新建会话并继续对话，而不打断原来的运行。

后端已经具备该语义，本次不改后端。

- PR #24（`d5f1825`）把生成锁从「用户」下沉到「单个对话」（`webui_new/manager.py:750-754`），
  只读接口不再取协调锁。
- 迁移 `0024_concurrent_sessions.sql` 删除了 `uq_conversation_sessions_active_user`
  ——"一个用户一个活跃会话"的限制已不存在。
- `context/memory_repository.py:194` 的 `create_session()` 明确"新建独立对话、不关闭其他对话"。
- 同一对话内并发是设计性的 409 `SESSION_BUSY`（`manager.py:767-768`），与 ChatGPT 一致。
- Supervisor 无状态，每轮 new 一个 `Turn`（`agent_runtime/engine.py:71-74`）。

阻塞点全部在前端。`webui_new/static/app.js` 是单例全局状态机，`createNewSession`
（`:1434`）、`openSession`（`:1451`）、`sendMessage`（`:2183`）三处在 `isProcessing` 时直接
`return`；流式 chunk 直接追加到全局 `#chatMessages`（`:2297-2304`），没有按会话分桶。

`docs/bugs/2026-09-15-multi-window-session-blocking.md:35` 已记录该边界：
"同一页面仍只维护一个正在生成的界面；本次交付的是多个窗口/标签页独立会话并发"。

### 现状量化

| 全局状态 | 引用数 |
| --- | --- |
| `chatMessages` | 26 |
| `activeSessionId` | 24 |
| `isProcessing` | 23 |
| `currentRequestId` | 13 |
| `interruptPending` | 6 |

另有 6 个独立卡片脚本由 `webui_new/templates/chat.html:469-474` 单独加载。
侧边栏 `<aside id="sidebar">`（`chat.html:275`）和 `renderSessions()`（`app.js:1398`）已存在。

## 目标与非目标

**目标**

1. 一个页面内可同时运行多个会话（软上限 3），互不打断。
2. 会话列表显示每个会话的运行状态；跑完静默完成，切回去能看到完整结果。
3. 草稿（输入文本、待发附件）随会话切换而切换。

**非目标**

- 不做服务端流重放。刷新或关闭页面，进行中的流即丢失，重新打开只能看到已落库的部分。
  这是本次明确选择的语义（内存态 runtime 的自然结果）。
- 不改后端任何代码。
- 不处理 `agent_runtime/services.py:84-88` 的跨会话记忆检索。该行为是**有意设计**
  （同文件 `:92` 注释："historical search belongs to the memory specialist"），
  并行后新增的副作用是 A 会话检索时可能读到 B 会话刚写入的消息。记为已知交互。

## 契约

新增 `webui_new/static/session-runtime.js`，暴露 `window.HommeySessionRuntime`，
在 `chat.html` 中于 `app.js` 之前加载（与现有 6 个卡片脚本同一套模式）。

该文件只负责**状态与生命周期**，不负责渲染。渲染仍由 `app.js` 完成。

```
SessionRuntime {
  id: string,
  container: HTMLElement,     // <div class="session-view">，活跃时挂载到 #chatMessages
  live: boolean,              // 等价于 processing；见下方保留规则
  processing: boolean,        // 取代全局 isProcessing
  requestId: string,          // 取代全局 currentRequestId
  interruptPending: boolean,  // 取代全局 interruptPending
  followConversation: boolean,// 滚动跟随，取代全局 followConversation
  retryRequestPending: boolean,
  submissionRetry: object|null,
  statusQueue: string[], statusTimer, lastStatusAt,   // 处理状态提示
  draft: { text: string, attachments: [], placeholder: string },
}
```

注册表接口：

```
HommeySessionRuntime.create(id)      -> SessionRuntime
HommeySessionRuntime.get(id)         -> SessionRuntime | null
HommeySessionRuntime.ensure(id)      -> SessionRuntime
HommeySessionRuntime.active()        -> SessionRuntime | null
HommeySessionRuntime.setActive(id)   -> SessionRuntime
HommeySessionRuntime.runningCount()  -> number
HommeySessionRuntime.discard(id)     -> void   // 销毁容器与内存态
```

**约定**：`activeSessionId` 这个全局保留，但它退化为"当前显示哪个 runtime"的指针，
不再承载任何会话状态。所有会话状态从 `active()` 取。

## 架构

### 容器模型

`#chatMessages`（`chat.html:125`）从"唯一容器"降级为**挂载点**。每个会话拥有自己的
`<div class="session-view">`：

- 切换会话：`#chatMessages` 内的 view 由 `.is-active` 决定谁可见，不做 detach/reattach
- 保留规则（**`live` 就是 `processing`**，不引入第二个标志）：
  - 切走时 `runtime.processing === true` → 保留引用，容器收起来但不销毁
  - 切走时 `runtime.processing === false` → `discard()`，下次打开按现有逻辑从库里重建

**为什么是隐藏而不是移除**：卡片脚本用 `isConnected` 判断生死。
`journey-map.js:28` 的 `MutationObserver` 在卡片脱离文档时调 `dispose()`，而
`dispose()` 是终态的（`disposed=true`、撤销 object URL、不再重新拉图）。
真把 view 摘下来，切走再切回时会得到一个已经退休的地图卡片。
隐藏则让 `isConnected` 保持为真，卡片的生命周期判断回到它原本的语义：
只有容器被丢弃（会话删除/清空/冷会话切走）才算移除。

常驻 view 上界：最多 3 个 live（并发上限）+ 1 个正在查看的 cold，共 4 个。
正在查看的就是某个 live 时更少。

这条规则依赖一个前提：运行结束后结果已落库（`sendMessage` 在 `done` 之后已经调
`refreshSessionList()`），所以丢弃容器后重建能拿回同样内容。

### 卡片脚本

6 个卡片脚本经核查基本无需改动，它们返回元素、由 `app.js` 负责挂载：

- `window.HommeyAnswerCard.create(documentData)` → 元素
- `window.HommeyTripIntakeCard.create(documentData)` → 元素
- `window.ExecutionPlan.update(container, plan)` → **已经接收容器参数**，只是现在传的是全局
  `chatMessages`，改为传 `runtime.container`
- `trip-choices.js:196` 已有 `!list.isConnected` 守卫，说明作者考虑过脱离 DOM 的场景

`journey-map.js` 不是 Leaflet，而是自绘的高德瓦片图，并且已经内建了脱离检测
（`journey-map.js:28`）。上面「隐藏而不是移除」的取舍就是为了让它不必知道会话切换这件事。

## 数据流

### 发送

`sendMessage()` 从"操作全局状态"改为"操作 runtime"：

1. 取 `runtime = ensure(activeSessionId)`
2. 门禁改为 `runtime.processing`（只挡本会话的重复提交，不挡其他会话）
3. 渲染目标改为 `runtime.container`
4. `isProcessing = true` → `runtime.processing = true`，结束时在 `finally` 复位
5. 请求体里的 `session_id` 从 `runtime.id` 取，而非全局 `activeSessionId`
   （后台会话在用户切走后完成请求时，这是关键）

`app.js:2297-2304` 的 chunk 追加、`:2269/:2272/:2312/:2313` 的 `ExecutionPlan.update`、
`:2349` 的 `connectionLost`、`:2341` 的 `renderAttachmentCards` 全部改指向
`runtime.container`。

### 处理状态指示器

`processingIndicator` 目前用全局 DOM id（`app.js:2516` 设 `row.id`，
`removeProcessingIndicator` 用 `document.getElementById`）。三个会话同时跑会有三个同 id
元素互相踩。改为在 `runtime.container` 内用 class 查询。

### 切换会话

`openSession(id)`：

1. 移除 `isProcessing` 门禁（`:1451`）
2. `ensure(id)`；若该 runtime 已有容器且 `live`，直接挂载，**不重新拉历史**
3. 否则按现有逻辑从 `GET /sessions/{id}/activate` 重建，并把结果容器登记到 runtime
4. 交换草稿：存回旧 runtime 的 `draft`（输入文本 + 待发附件），载入新 runtime 的 `draft`，
   并重画附件区
5. `setActive(id)`，重画侧边栏高亮

首页（还没进任何会话）也有输入区和附件，它先落在一个匿名草稿位上，进会话时随
`adopt` / `mountSession` 搬进那个 runtime，不会丢也不会串到别的会话。

`createNewSession()`（`:1434`）同样移除门禁，走同一套挂载/交换逻辑。

### 运行完成

后台会话完成时（用户正在看别的会话）：

1. 容器的 DOM 操作照常进行——容器是脱离 DOM 的节点，`appendChild` 等仍有效
2. `scrollToBottom()` 需要 `isConnected` 守卫，避免对脱离节点滚动
3. 更新侧边栏该会话的运行指示（去掉）
4. 调 `refreshSessionList()` 刷新标题/预览

### 并发上限

`runningCount() >= 3` 时，`sendMessage` 立即弹提示"最多同时跑 3 个会话，等一个完成再开"，
不发起请求。

依据：后端全局信号量 8（跨用户共享）、I/O 池 16 workers / 32 pending、单轮 60s 超时。
不设前端上限的失败模式是撞上信号量后无反馈地等 120s 再报 `GLOBAL_CONCURRENCY_LIMIT`。

### 侧栏运行指示

`renderSessions()`（`:1398`）里，对 `HommeySessionRuntime.get(session.session_id)?.processing`
为真的行加一个最小指示（小圆点 / 转圈）。按仓库既有前端取向：信息克制、不占版面。

发送键语义：仅当**当前查看的**会话在跑时，发送键变停止键；其他会话不受影响。
`interruptCurrentTurn()`（`:2405`）改为从 `active()` 取 `requestId`，不再用全局
`currentRequestId`。

## 错误处理

| 场景 | 行为 |
| --- | --- |
| 后台会话流式失败 | 错误渲染进**该会话自己的**容器，不打断当前查看的会话；侧栏指示转为错误态 |
| 切到正在跑的会话 | 显示已有的处理指示器与已收到的 chunk，不重置流 |
| 达到并发上限 | 立即提示，不发请求 |
| 删除正在跑的会话 | 先 `interruptCurrentTurn()` 再删；现有 `SESSION_BUSY` 409 作为兜底 |
| 刷新页面 | 内存 runtime 丢失；进行中的运行在服务端继续，重开后只能看到已落库部分 + 侧栏无指示 |

最后一条是有意取舍，已在目标的非目标里声明。

## 测试

**扩展 `scripts/check_session_concurrency_ui.py`**（现有 Playwright 无头测试台，
API 全打桩、不调模型）。新增单页并行场景：

1. 会话 A 发消息 → 流保持打开
2. 不中断的情况下新建会话 B → **断言 B 的消息成功发出**（当前会被 `isProcessing` 挡掉）
3. 在 B 中发消息 → 断言两个流同时打开
4. 此时 `createNewSession` 仍可用
5. 切回 A → 断言 A 的容器重新挂载，且 A 的已收 chunk 仍在
6. 关闭 A 的流 → 断言 A 的侧栏指示消失、B 不受影响
7. 选第 4 个会话发消息 → 断言弹提示且未发起请求

**回归**：现有双标签页用例必须继续通过；`refreshSessionList` 的错误路径
（`app.js:1359-1377`）不受影响。

**手工验证**（无头测试覆盖不到的）：带地图卡片的会话切走再切回后瓦片是否正常显示。
无头测试只能断言画布始终连着文档（`document.contains`），真实瓦片要联网才看得见。

## 影响面

| 文件 | 改动 |
| --- | --- |
| `webui_new/static/session-runtime.js` | 新增 |
| `webui_new/templates/chat.html` | 加 script 标签 |
| `webui_new/static/app.js` | 移除 3 处 `isProcessing` 门禁；26 处 `chatMessages` 改指 runtime 容器；5 个全局状态迁入 runtime；指示器 id 改 class |
| `scripts/check_session_concurrency_ui.py` | 新增单页并行场景 |
| `docs/bugs/2026-09-15-multi-window-session-blocking.md` | 更新"同一页面只维护一个界面"这条已知边界 |

后端、迁移、6 个卡片脚本、`app.js` 的卡片渲染辅助函数均不改。
