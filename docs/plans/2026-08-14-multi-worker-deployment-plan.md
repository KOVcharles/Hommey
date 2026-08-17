# 云端多进程部署、并发一致性与 PostgreSQL 存储收敛计划

> 状态：持续维护中的主计划；设计、实施进度、验收结果和后续改进统一记录在本文。
> 关联：`docs/superpowers/specs/2026-08-04-global-concurrency-hardening-design.md`。
> 文档约定：完成项使用删除线并标注日期；不再为每次并发升级新建报告。

## 当前结论（2026-08-17）

阶段一 Web 并发整改、阶段二 PostgreSQL VectorStore 和精简版阶段三持久化刷新控制面已完成。项目默认配置及当前 Compose 运行配置均已收敛为长期 PostgreSQL、RAG PostgreSQL + pgvector，Compose 的短期协调状态使用 Redis；Milvus Lite 的 164 个 chunk 已迁移并发布为 pgvector active version，仅保留迁移兼容入口。

当前同一云主机 Compose 已运行 `UVICORN_WORKERS=2`，并由独立 `rag-worker` 执行 PostgreSQL 持久化刷新任务。知识库源文件和附件暂时使用同主机共享持久卷，因此当前结论只覆盖“单主机双 worker”；扩展到跨主机 Web 副本前仍需接入对象存储，并完成阶段四剩余故障注入。

## 1. 背景

Hommey 当前以单个 uvicorn worker 运行。Redis 分布式协调和 PostgreSQL 持久化能力已经具备，但仍有以下因素阻塞安全的多进程和云端部署：

- ~~RAG 使用 Milvus Lite 单文件库，多个进程不能安全共享。~~（2026-08-17 已迁移到 PostgreSQL + pgvector；Milvus Lite 仅保留迁移兼容）
- ~~Web 层仍依赖进程内热用户实例，部分状态接口未进入统一用户锁。~~（2026-08-17 已完成冷 worker 恢复和用户状态作用域）
- ~~知识库上传锁、刷新任务和刷新状态仍是进程内状态。~~（2026-08-17 已迁移到 PostgreSQL source generation、任务队列、租约和 worker 心跳）
- ~~Redis 全局信号量使用单计数器加 TTL，长请求可能跨过 TTL 后突破并发上限。~~（2026-08-17 已改为 token 化 ZSET 租约）
- ~~RAG、embedding 和同步 PostgreSQL I/O 可能阻塞事件循环，导致锁心跳无法按时续约。~~（2026-08-17 已接入应用自有有界 executor）
- 知识库源文件、附件和 manifest 使用本地目录，不能直接支持多个云端副本。

因此，本计划不把“将 `UVICORN_WORKERS` 改为 2”视为完成条件。完成条件是：所有权威状态可共享、并发约束可验证、进程故障可恢复，并能在两个独立进程或容器之间通过验收。

## 2. 目标与非目标

### 2.1 目标

- 支持至少两个应用进程安全处理请求。
- 推荐生产拓扑为两个 Web 副本、每个副本一个 uvicorn worker；单机部署也允许一个容器内两个 worker。
- 保证聊天、会话、记忆、偏好、行程、编排和知识库管理在跨 worker 场景下一致。
- 使用 PostgreSQL 16 + pgvector 存储 RAG 文档、chunk、向量、索引版本和刷新任务。
- 使用带租约和持有者 token 的 Redis 原语进行跨进程协调。
- 将知识库刷新改为可持久化、可续租、可在 worker 崩溃后恢复的后台任务。
- 云端多副本使用对象存储或可靠共享文件系统保存知识库源文件和附件。
- 建立可量化的并发、故障恢复、性能、备份和回滚验收门槛。

### 2.2 非目标

- 当前数据规模下不部署 Milvus Standalone。
- 首期不创建 HNSW 或 IVFFlat 近似向量索引。
- 不在本阶段把所有 psycopg 同步 repository 重写为 asyncpg。
- 不依赖负载均衡器 sticky session 保证正确性；sticky session 只能作为性能优化。
- 不在首期实现 Kubernetes 专用控制器或复杂工作流引擎。

## 3. 正确性原则

### 3.1 权威状态与协调状态分离

PostgreSQL 是权威持久化数据源，承载：

- 用户、鉴权、会话和消息。
- 长期记忆、偏好、行程和编排状态。
- RAG 文档、chunk、向量、索引版本和刷新任务。
- 技能配置、评估、审计和需要可靠保留的运行记录。

Redis 只承载：

- 同用户操作锁。
- 全局并发租约。
- 熔断、限流和短期缓存。
- 可丢失、可从 PostgreSQL 恢复的临时信号。

Redis 锁不能代替数据库约束。所有关键写入仍需使用 request id、唯一约束、revision 或条件更新保证幂等和防止丢失更新。

### 3.2 进程内状态只能是缓存

`WebHommeyManager._instances`、agent cache、embedding cache 和本地锁可以保留，但不得作为接口正确性的前提：

- 任意请求落到一个从未见过该用户的 worker 时，必须能够从共享存储恢复。
- 会话列表、active session 和用户状态必须从 PostgreSQL 获取或在持锁后重新同步。
- 一个 worker 修改状态后，另一个 worker 不能长期返回旧状态。
- 进程重启只能造成缓存冷启动，不能造成业务数据丢失。

2026-08-17 已落地 `get_initialized_user()`：session、onboarding、用户摘要和 active trip 请求落到冷 worker 时会重建本地运行时。该能力以 Redis/PostgreSQL 共享存储为前提；当前默认和 Compose 生产配置已使用这些共享后端。

### 3.3 Redis 故障时 fail closed

Redis 无法获取锁或租约时，不允许绕过协调继续执行昂贵请求或用户状态写入。接口返回可重试的 503；readiness 同时失败。已经开始的操作在心跳失败后停止后续业务提交，并依靠数据库幂等约束处理无法立即取消的同步 I/O。

2026-08-17 已落地：协调原语获取失败返回可重试错误，`/readyz` 在必需检查失败时返回真实 HTTP 503，而不是 `200 {ok:false}`。

## 4. 目标云端拓扑

推荐生产拓扑：

```text
                         ┌─ Web replica 1（uvicorn worker=1）
Internet ─ TLS/LB ───────┤
                         └─ Web replica 2（uvicorn worker=1）
                                  │
                 ┌────────────────┼────────────────┐
                 │                │                │
        PostgreSQL 16       Redis 7          对象存储/共享卷
          + pgvector      锁/租约/缓存       文档/附件/导出
                 │
          RAG job worker
       持久化任务认领与刷新
```

`2 replicas × 1 worker` 优先于 `1 replica × 2 workers`，因为前者还提供进程、容器和滚动发布级别的故障隔离。若首期只能使用一台云主机，可先运行一个应用容器内两个 worker，但必须明确：这解决并发共享问题，不提供主机级高可用。

生产环境要求：

- PostgreSQL 和 Redis 不直接暴露公网端口。
- 数据库、Redis、对象存储和外部模型密钥由云端 secret manager 或等价设施注入。
- 使用 TLS、私有网络和最小权限账号。
- 不允许沿用 Compose 中的默认数据库密码。
- 应用镜像以不可变 tag 或 digest 发布，数据库和 pgvector 版本固定。

## 5. 并发控制设计

### 5.1 统一用户操作入口

`WebHommeyManager` 增加统一的用户操作作用域，所有修改用户状态的入口都必须经过它：

- 普通聊天与流式聊天。
- 创建、激活、重命名、删除和清空会话。
- onboarding 偏好写入。
- 行程状态修改和编排中断。
- 其他会修改用户级状态的接口。

固定顺序：

1. 进程内 per-user lock，用于减少本进程 Redis 争用。
2. Redis per-user lease lock，用于跨 worker 串行。
3. 持锁后从 PostgreSQL 重新同步 active session 和必要版本。
4. 只有聊天、LLM、RAG 等昂贵操作继续获取全局并发租约；普通会话元数据写入不占用全局 LLM 配额。
5. 完成后逆序释放。

只读接口不必全部加分布式锁，但不得依赖当前 worker 是否已有用户实例。

2026-08-17 已落地 `user_state_scope()` 和 `run_user_state_operation()`：会话创建、激活、重命名、删除、清空以及 onboarding 写入均使用跨 worker 用户锁，短状态操作不占全局 LLM 槽位。`interrupt` 刻意绕过长锁，因为活跃 stream 正持有该锁；它通过 durable run 的 request id、turn id 和状态冲突检查拒绝陈旧中断。

### 5.2 同用户分布式锁

锁必须包含唯一 token，并通过 Lua 原子执行：

- 获取：`SET key token NX PX ttl`。
- 续约：仅当 key 当前值仍等于 token 时延长 TTL。
- 释放：仅当 key 当前值仍等于 token 时删除。
- 心跳间隔小于 TTL 的三分之一。
- 心跳失败时设置 `lock_lost`，停止后续业务提交。

2026-08-17 已修复流式窗口：外层同时等待下一条 stream event 和 `lock_lost`；即使内层阻塞在队列等待，锁丢失也会立即取消待定读取并关闭生成器。

同步阻塞操作不得运行在事件循环线程中，否则心跳无法得到调度。RAG 查询、同步 embedding 和同步 PostgreSQL 操作必须通过专用受限线程池执行，或改为原生异步实现。

### 5.3 严格租约式全局信号量

废弃“单计数器 + 整体 TTL”的实现，改为 token 化租约：

- Redis ZSET 的 member 是每个持有者唯一 token，score 是租约过期时间。
- 获取脚本先用 Redis 服务端时间删除过期 token，再检查 `ZCARD < max_concurrency`，成功后加入当前 token。
- 续约脚本只有在 token 仍存在时更新过期时间。
- 释放脚本只删除自己的 token，不影响其他请求。
- 持有期间持续续约；续约失败时停止后续业务提交。

必须测试请求执行时间超过一个租约周期时，并发数仍不超过上限。Redis 重启或主从切换可能丢失协调租约，因此数据库幂等和 revision 仍是最终安全边界。

### 5.4 同步 I/O 与取消

- RAG embedding 当前使用同步 HTTP，请求和重试必须离开事件循环。
- PostgreSQL 同步 repository 使用应用自有 executor，防止默认线程池被放大。
- 所有外部请求必须有连接、读取和总超时。
- 取消协程不能强制停止已经在线程中执行的同步调用；因此线程任务只允许执行幂等读，或通过 request id、唯一约束和条件更新保护写入。
- SSE 断连、应用 shutdown 和锁丢失都必须走同一清理路径。

2026-08-17 已落地真正的提交背压。仅设置 `ThreadPoolExecutor(max_workers=N)` 并不会限制其内部等待队列；当前实现用 `BoundedSemaphore` 将运行中与排队任务总数限制为：

```text
HOMMEY_IO_EXECUTOR_MAX_WORKERS + HOMMEY_IO_EXECUTOR_MAX_PENDING
```

容量耗尽时快速返回 `IO_EXECUTOR_SATURATED`。permit 在底层同步任务真正结束时释放，避免 asyncio 等待方被取消后提前虚报容量。应用退出时先等待 executor 收尾，再关闭 PostgreSQL pool。

## 6. PostgreSQL + pgvector RAG 设计

### 6.1 选择 pgvector

当前知识库约 14 个文档、164 个 chunk、1024 维向量。首期采用精确余弦检索：

```sql
ORDER BY embedding <=> %(query_embedding)s::vector
LIMIT %(top_k)s
```

当前规模不创建近似索引。只有当真实语料、QPS 和延迟指标达到触发条件后，才评估 HNSW。

### 6.2 数据模型

新增幂等迁移，启用 `vector` 扩展，并建立以下逻辑表：

- `rag_collections`：collection 名称、active version、embedding 模型和维度。
- `rag_index_versions`：版本、状态、构建时间、激活时间、chunk 数、构建配置和错误。
- `rag_documents`：源文件标识、对象存储 key、内容 hash、文档版本、解析器版本和索引状态。
- `rag_chunks`：collection、index version、chunk id、document id、document version、正文、metadata、`VECTOR` 和写入时间；应用按版本声明的维度校验，本次 active version 为 1024 维。
- `rag_refresh_jobs`：任务状态、requested_by、source generation、进度、lease owner、lease expiry、attempt、报告和错误。

关键约束：

- `(collection, index_version, chunk_id)` 主键或唯一约束。
- `(collection, index_version, document_id, document_version, chunk_hash)` 幂等唯一约束。
- 每个 collection 最多一个 active version。
- embedding 模型、维度和索引指纹必须随版本保存；配置不匹配时拒绝查询并要求重建。
- 向量写入前验证数量、维度和有限数值。

### 6.3 版本化原子发布

全量刷新不直接删除 active version：

1. 读取任务创建时冻结的 source generation。
2. 在数据库事务之外完成文档读取、解析、切分和向量生成。
3. 将新版本标记为 `building` 并写入新版本 chunk；查询仍只读取旧 active version。
4. 校验文档数、chunk 数、embedding 维度、索引指纹和 golden queries。
5. 使用一个短 PostgreSQL 事务锁定 collection，退役旧 active version，激活新版本，同时写入刷新结果和 manifest 元数据。
6. 任一步失败都将新版本标记为 `failed`，旧 active version 保持不变。

检索 SQL 必须在同一条查询中解析 active version，避免先读版本再查 chunk 的竞态。旧版本保留一个回滚窗口后再异步清理。

文件形式的 `ingestion_manifest.json` 仅可作为导出或兼容副本，不再是权威状态。数据库版本切换成功但文件写入失败时，不能影响线上索引状态。

### 6.4 VectorStore 接入

- 增加 `PostgresVectorStore`。
- 增加统一 `create_vector_store(config)` 工厂；`RAGPipeline`、`KnowledgeRetriever`、CLI 和 Web 刷新全部通过工厂构造。
- 配置项 `HOMMEY_RAG_VECTOR_BACKEND` 支持 `postgres`、`milvus_lite` 和 `memory`。
- 生产和所有多进程环境只允许 `postgres`。
- preflight 在 worker 数大于 1 且后端不是 PostgreSQL 时直接失败。

BM25、RRF、rerank、evidence filter 和 HyDE 从 Milvus 适配器中提取到共享检索层：

- dense 分支由 VectorStore 返回候选。
- BM25 首期读取当前 active version 的全部 chunk，在 Python 中计算；当前规模可接受。
- 所有分支必须使用同一个 active index version。
- 首期不使用未带版本校验的进程内 BM25 缓存，避免刷新后 worker 返回旧结果。

## 7. 知识库源文件与刷新任务

### 7.1 源文件存储

推荐使用对象存储保存：

- 知识库源文件。
- 用户附件。
- 导出产物。

数据库保存对象 key、etag/hash、大小、上传者和版本。上传使用不可变对象 key，并通过数据库唯一约束处理同名和幂等冲突。

如果首期使用单云主机，可临时使用同一容器内的共享持久卷；此模式只允许一个应用副本，两个 worker 共享同一挂载。切换到两个容器副本前必须完成对象存储或 ReadWriteMany 共享卷改造。

### 7.2 上传与刷新快照

进程内 `_source_lock` 不再承担正确性：

- 每次源文件变更提交一个递增的 source generation。
- 创建刷新任务时冻结当前 generation。
- 刷新只读取该 generation 对应的不可变文件集合。
- 刷新期间的新上传进入下一 generation，并明确显示为 `pending`，不污染正在构建的索引。
- 同名上传由数据库唯一约束和幂等 key 决定，不使用 check-then-write 文件判断。

### 7.3 持久化任务执行

Web API 只创建 `rag_refresh_jobs` 记录并返回 202，不再启动 daemon thread。

独立 RAG job worker：

- 使用 `FOR UPDATE SKIP LOCKED` 或等价原子更新认领任务。
- 同一 collection 通过数据库约束和 Redis single-flight lock 保证最多一个 active refresh。
- 定期更新 `lease_owner`、`lease_expires_at`、stage 和 progress。
- worker 崩溃后，过期任务可由其他 worker 重试。
- attempt 超过上限后标记失败并保留旧 active index。
- 应用滚动发布不会丢失刷新状态。

2026-08-17 已落地：Web API 以 HTTP 202 创建 `rag_refresh_jobs`；worker 使用 `FOR UPDATE SKIP LOCKED`、collection advisory lock 和可续租 owner 执行任务；过期租约可重新认领，陈旧 owner 无法提交结果。`rag_worker_heartbeats` 进入 readiness，worker 缺失或心跳过期时 `/readyz` 返回 503。

## 8. 数据库连接与迁移

### 8.1 连接预算

上线前必须写出连接预算，而不是只确认“连接数够用”：

```text
web_connections = replicas × uvicorn_workers × postgres_pool_max_size
total_connections = web_connections
                  + rag_job_worker_pool
                  + evaluation_worker_pool
                  + migration/admin/reserved_connections
```

初始建议：

- 两个 Web 副本，每副本一个 worker。
- 每个进程 PostgreSQL pool max 设为 5，而不是默认 10。
- RAG job worker 独立小连接池。
- 预计峰值连接总数不超过数据库 `max_connections` 的 70%，为迁移、监控和故障恢复预留空间。
- 审计并逐步移除绕过共享 pool 的临时直连；连接数扩大后再评估 PgBouncer。

### 8.2 迁移策略

- 云数据库必须在部署前确认允许启用 pgvector；`CREATE EXTENSION vector` 所需权限在基础设施阶段处理。
- 推荐将迁移作为一次性 release job 在新版本 Web 启动前执行。
- 当前 session-level advisory lock 继续作为重复启动保护，但不把每个 Web worker 自行迁移作为长期部署方式。
- 所有迁移遵循 expand/contract，确保滚动发布期间新旧应用版本可短暂共存。
- 迁移必须幂等并保持 checksum 校验。

## 9. 可观测性与运行保护

必须暴露或采集以下指标：

- 当前用户锁等待数、等待时间、超时数和锁丢失数。
- 全局租约使用量、获取等待、续约失败和过期清理数。
- 每个 worker 的事件循环延迟和线程池队列深度。
- PostgreSQL pool 使用量、等待时间、慢查询和连接错误。
- RAG embedding、dense 查询、BM25、rerank 分阶段延迟。
- active index version、刷新任务状态、lease 年龄和失败次数。
- HTTP QPS、p50/p95/p99、4xx、5xx 和 SSE 取消数。

当前内存 metrics sink 是每进程独立的。多 worker 下必须改为外部指标后端、OpenTelemetry，或正确配置 Prometheus multiprocess mode，不能让 `/metrics` 随机返回某一个 worker 的局部数据。

健康检查分层：

- `/healthz`：仅表示进程存活，不访问外部依赖。
- `/readyz`：检查 PostgreSQL、pgvector schema、active RAG version、Redis 和所需存储；失败时从负载均衡摘除。
- 外部 embedding/LLM 健康作为独立观测项，避免短暂供应商抖动导致所有实例同时重启。

## 10. 实施阶段

### 阶段零：冻结基线和上线门槛

- 记录单 worker 的 RAG golden queries、召回结果和分阶段延迟。
- 记录聊天吞吐、LLM 并发数、数据库连接峰值和错误率。
- 准备 PostgreSQL 备份、源文件备份和实际恢复演练。
- 确定首期云平台、网络、域名、TLS、secret 和对象存储方案。

### 阶段一：先修复并发正确性

- ~~将全局信号量替换为 token 化 ZSET 租约并增加心跳。~~（2026-08-17 完成）
- ~~将同步 RAG/embedding、memory 和 orchestration state I/O 移出事件循环。~~（2026-08-17 完成）
- ~~为应用自有 executor 增加真正有界的提交队列和满载拒绝。~~（2026-08-17 完成）
- ~~把会话和 onboarding 等用户状态写接口纳入统一 per-user 分布式锁。~~（2026-08-17 完成）
- ~~让用户相关接口能够在当前 worker 没有热实例时恢复。~~（2026-08-17 完成；要求 Redis/PostgreSQL 生产配置）
- ~~补齐 request id 唯一约束、编排 revision/冲突的代码级测试。~~（2026-08-17 完成）
- ~~Redis 不可用时 fail closed，并让 readiness 返回 HTTP 503。~~（2026-08-17 完成）
- ~~修复流式生成器阻塞期间无法及时发现锁丢失的问题。~~（2026-08-17 完成）
- ~~在真实 Redis 环境运行 `test_redis_coordination.py`、`test_concurrency_e2e.py`。~~（2026-08-17 完成，10 passed）
- ~~在设置 `HOMMEY_TEST_POSTGRES_DSN` 的环境运行 PostgreSQL memory/pgvector 集成测试。~~（2026-08-17 完成，13 passed、1 skipped）

阶段一代码验收通过：

```text
并发、I/O、readiness、Web API 契约：39 passed
编排恢复、异步记忆、memory 幂等：33 passed, 1 skipped
```

阶段一完成时曾因刷新任务和本地文件状态而继续保持单 worker；该限制已由精简版阶段三解除，当前同主机共享卷模式已切换为两个 worker。

### 阶段二：落地 PostgreSQL VectorStore

- ~~启用 pgvector 并创建版本化 RAG collection、index version、document 和 chunk 表。~~（2026-08-17 完成）
- ~~实现精确余弦向量写入、查询、统计和原子 active version 发布。~~（2026-08-17 完成）
- ~~引入 `create_vector_store()` 工厂，生产 RAG/CLI/Web 路径不再直接构造 Milvus。~~（2026-08-17 完成）
- ~~提取共享的 BM25、RRF、rerank、filter 和 HyDE dense 检索流程。~~（2026-08-17 完成）
- ~~为同步 PostgreSQL 和 embedding 使用阶段一的有界 executor 与超时。~~（2026-08-17 完成）
- ~~preflight 验证 PostgreSQL、pgvector 扩展、collection、模型、维度、fingerprint 和 active version。~~（2026-08-17 完成）
- ~~提供 Milvus Lite → PostgreSQL 一次性迁移工具并核对源/目标数量。~~（2026-08-17 完成，164 → 164）
- ~~Compose 开发/测试数据库固定为 `pgvector/pgvector:0.8.6-pg16-bookworm`。~~（2026-08-17 完成）
- ~~将代码默认值、`.env.example` 和当前 `.env` 的长期/RAG 后端收敛为 PostgreSQL，Milvus Lite 不再作为隐式默认值。~~（2026-08-17 完成）

阶段二实际验收：迁移重复执行 `applied=0`；pgvector `0.8.6`；active index 维度 1024、chunk 数 164；`/readyz` 全部通过；真实 PostgreSQL dense + BM25/RRF 查询返回预期住宿政策。RAG/可观测性回归 61 passed，隔离 PostgreSQL 集成测试 13 passed、1 skipped，真实 Redis 并发测试 10 passed。切换前数据库备份保存在 `data/backups/pre-pgvector-phase2-20260817.sql`。

### 阶段三：持久化刷新控制面

- ~~将 manifest、刷新状态和进度迁入 PostgreSQL。~~（2026-08-17 完成；本地 manifest 仅作兼容导出）
- ~~将生产路径的 daemon thread 改为持久化任务和独立 job worker。~~（2026-08-17 完成）
- ~~引入 source generation 和每个任务冻结的文件 hash 快照。~~（2026-08-17 完成）
- ~~知识库源文件和附件使用同主机共享持久卷，并明确限制部署边界。~~（2026-08-17 完成；跨主机副本前仍需对象存储）
- ~~验证租约过期重新认领、陈旧 owner 拒绝提交、worker 心跳失效使 readiness 返回 503。~~（2026-08-17 完成）

阶段三实际验收：两个 Web worker PID `9`、`10` 均实际接收 HTTP 请求；独立 `rag-worker` 心跳进入 PostgreSQL；停止 worker 后 `/readyz` 返回 503，恢复后自动返回 200；真实 PostgreSQL 并发认领、租约接管和 stale-owner 测试通过。阶段一至三相关合并回归为 112 passed。

### 阶段四：真实多进程验证

- ~~启动两个真实 uvicorn worker，并通过 `/healthz` 的不同 PID 确认二者均接收请求。~~（2026-08-17 完成）
- ~~补充车票查询部署烟测：未指定日期按 `Asia/Shanghai` 当天直接查询；“明天的”等日期型短回复继承上一轮车票路线，不误入 `event_collection`。~~（2026-08-17 完成；真实 12306 查询与双 worker 重启后 readiness 已验证）
- 强制同一用户的并发请求分别命中两个实例。
- ~~使用两个独立 PostgreSQL 认领者并发竞争同一刷新任务，确认只允许一个 owner。~~（2026-08-17 完成）
- 强制知识库上传、刷新和状态查询分别命中不同 Web worker。
- 运行长于一个锁和信号量租约周期的请求。
- ~~注入 RAG worker 心跳过期并验证 readiness 摘流及恢复。~~（2026-08-17 完成）
- ~~终止一个真实 Web worker，确认父进程自动补回且 readiness 持续为 200。~~（2026-08-17 完成，PID 9 被 PID 215 替换）
- 注入 Redis 短暂不可用、PostgreSQL 连接耗尽和 embedding 超时。
- 检查全局并发数、重复写入、active version 和任务恢复结果。

### 阶段五：云端灰度上线

1. 部署 PostgreSQL/pgvector 迁移和共享存储。
2. 从源文档构建 PostgreSQL RAG 新版本并验证，但暂不删除 Milvus Lite 数据。
3. 先部署一个新版本 Web 副本，运行 smoke test 和 golden queries。
4. 扩展到两个 Web 副本，每副本一个 worker。
5. 观察完整回滚窗口后再删除旧 Milvus Lite 文件和依赖。
6. 完成备份恢复演练后，才将新拓扑标记为稳定生产版本。

## 11. 验收要求

### 11.1 并发正确性

- 同一用户的两个请求分别命中两个实例时，业务临界区最大并发数始终为 1。
- 不同用户可以并行处理。
- 请求持续时间超过租约周期时，全局并发数仍不超过配置上限。
- 锁或租约续约失败后，不再提交新的业务状态。
- 重复 request id 不产生重复用户消息、助手消息、行程更新或编排 run。
- 会话创建、激活、删除和聊天并发时，不出现两个 active session 或 worker 间长期状态分叉。

### 11.2 RAG 一致性

- 两个实例查询到相同 active index version。
- 刷新过程中旧版本持续可读，成功后所有实例一次性看到新版本。
- 解析、embedding、写入、校验或发布失败时旧版本不变。
- PostgreSQL 中的文档数、chunk 数、模型、维度和索引指纹一致。
- golden queries 的召回质量不低于迁移前基线。
- BM25、dense 和 HyDE 分支不会混用不同 index version。

### 11.3 故障恢复

- 刷新 worker 在任务中途被杀后，lease 过期可由另一个 worker 接管或明确失败。
- Web worker 在持锁期间被杀后，锁在租约到期后可恢复，数据库无重复最终状态。
- Redis 不可用时请求 fail closed，不会绕过全局并发限制。
- PostgreSQL 短暂不可用时 readiness 失败，恢复后无需人工重启即可重新就绪。
- 使用实际备份成功恢复 PostgreSQL 和源文件，而不只验证备份命令返回成功。

### 11.4 初始性能门槛

以下数值作为首轮压测门槛，可根据阶段零基线调整，但必须在上线前冻结：

- 精确向量 SQL 查询本身 p95 小于 100 ms、p99 小于 250 ms，不含外部 embedding。
- 非 LLM 普通 API p95 小于 300 ms。
- 稳态 5xx 比例小于 1%。
- PostgreSQL 峰值连接数不超过上限的 70%。
- 线程池无持续增长的排队，事件循环延迟不因同步 RAG 请求出现长时间尖峰。
- 压测结束后 Redis 中不存在无法由租约自动清理的锁或信号量 token。

## 12. 回滚方案

- 应用回滚：保留上一个不可变镜像，数据库迁移保持向后兼容，切回旧镜像不执行破坏性 schema 回退。
- RAG 回滚：将 collection 的 active version 原子切回上一个已验证版本。
- 数据库回滚：重大故障使用已验证备份恢复，不通过替换 Compose 镜像假装回滚数据。
- 文件回滚：对象存储启用版本控制，或保留带内容 hash 的不可变对象。
- Redis 不需要作为数据恢复源；重启后从 PostgreSQL 恢复权威状态。

## 13. 后续演进触发条件

1. 小规模继续使用 pgvector 精确检索。
2. 精确检索延迟持续超过已冻结 SLO 后，先评估查询优化和 HNSW。
3. PostgreSQL 资源竞争影响核心业务后，再评估读副本、PgBouncer、独立 RAG 数据库或资源隔离。
4. 只有在真实规模和压测证明 PostgreSQL 无法满足目标时，才重新评估专用向量数据库。
5. Web 吞吐增长后，可逐步把同步 repository 和 embedding 客户端迁移为原生异步实现。

本阶段的推荐结论是：**两个无状态 Web 进程 + Redis 租约协调 + PostgreSQL/pgvector 权威存储 + 持久化 RAG job worker + 共享对象存储**。
