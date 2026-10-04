# 代码精简排查报告

> 这是清理前的排查记录。已按确认执行清理，实际改动和验证结果见 [代码清理结果](2026-10-04-code-cleanup-result.md)。

排查日期：2026-10-04。范围为当前工作区，包含现有未提交改动；本次没有删除或修改业务源码。

## 结论

项目确实还有一批值得清理的代码，主要集中在架构替换后的旧实现、未接入的能力和兼容入口。当前主运行器、RAG 主链路和页面组件大部分仍有实际引用，不适合按目录大小或一次字符串搜索批量删除。

本次统计了 **160 个生产 Python 文件，共 26,545 行**。第一批列出的整文件及函数/类候选合计 **2,260 行 Python，约占 8.5%**；这是源码行数，包含注释和空行，尚未实际删除，也未计入对应测试、配置和少量 JavaScript。另有约 **660 行**旧摘要和旧画像仓储可以在第二批整理。

## 排查方法与边界

- 以 `webui_new/server.py` 为应用入口，以 `scripts/*.py` 为运维/开发入口，构建 AST 模块引用图。
- 检查函数、类、属性引用及字符串形式的调用；另外统计测试引用，区分业务代码、测试工具和只被旧测试使用的实现。
- 对候选重新用 `rg` 检查，阅读真实调用链及动态分发逻辑。
- 核查模板、JS、CSS、静态资源、依赖文件、配置读取、Docker 构建边界。
- 在现有 `hommey-app` 容器中运行 12 个相关测试文件作为抽样基线。

静态检查不能证明外部客户端从未调用某个 HTTP 接口，也不等于已经验证删除后的程序。因此下列内容是一份带证据的清理清单；涉及接口、数据库和历史数据的项目单独处理。

## 第一批：明确未被当前业务调用的实现

| 项目 | 位置与规模 | 证据 | 清理方式 |
| --- | --- | --- | --- |
| 旧 PostgreSQL autocommit 实现 | `context/long_term_memory.py:343`，`LegacyAutocommitPostgresLongTermMemory`，879 行 | 仅 `tests/test_preference_storage_v2.py` 创建它；文件末尾明确说明仅保留作迁移参考。实际运行走 `PostgresCompatibilityStore` 和共享连接池。 | 删除这个旧类，保留文件存储、禁用存储及当前构造器；把仍测旧类的偏好用例转到当前 pooled store。 |
| 未接入的 MCP 客户端包 | `hommey_mcp/`，3 个文件，559 行 | 包外没有 Python 导入或实例化。当前 `BusinessServices` 提供受控业务工具，未消费 MCP 管理器。 | 整包清理；同步移除无实际连接效果的配置和 readiness 检查，见下文。 |
| 旧 JSON 解析工具 | `utils/json_parser.py`，236 行 | 生产代码、脚本及测试均未导入它。当前 JSON 解析和模型文本提取使用 `core/json_output.py`、`core/llm_response.py`。 | 可整文件删除。 |
| 旧 HyDE 实现 | `rag/hyde.py`，190 行 | 只在 `tests/test_rag_hyde_mode.py` 中导入；当前制度查询只调用普通 `KnowledgeRetriever.search()`。 | 删除未接入实现及孤立单测；同步处理配置。界面上的增强模式是另外的产品入口，不能一起无声删除。 |
| 旧确定性证据判定器 | `rag/evidence.py`，197 行 | 只在 `tests/test_rag_phase4_evidence.py` 中导入，主检索及主 Agent 没有调用 `evaluate_evidence()`。 | 若以当前 Supervisor 的证据处理为准，可清理旧模块及专属测试；保留实际使用的来源回读、字段依据和权限校验。 |
| 同步熔断器 | `utils/circuit_breaker.py:25`，`CircuitBreaker`，98 行 | 当前工厂始终返回 `RedisCircuitBreaker`，同步类仅剩类型标注和未使用导入；旧 MCP 的熔断参数也未接入应用。 | 改正 `runtime.py` 和 `webui_new/manager.py` 的类型标注后删除同步类。**保留 `CircuitOpenError`**，Redis 熔断和重试仍使用它。 |

另外找到以下小型未引用定义，共 128 行 Python：

| 位置 | 未使用定义 | 行数 |
| --- | --- | --- |
| `config_agentscope.py:25` | `get_model_config()` | 16 |
| `core/onboarding.py:126` | `detect_city_from_ip()` | 20 |
| `core/trip_intake.py:81` | `remove_ungrounded_trip_locations()` | 22 |
| `utils/skill_loader.py:69` | `get_skill_prompt()` | 11 |
| `context/memory_manager.py:107` | `get_recorded_answer_document()`、`get_recorded_presentation_document()` | 19 |
| `multimodal/schemas.py:55` | `MessageInput` | 7 |
| `webui_new/core/errors.py:123` | Python `ApiError` 兼容包装类 | 6 |
| `core/onboarding.py:47` | `get_options()` | 7 |
| `context/short_term_memory.py:126` | `get_context_string()` | 20 |

上表最后两项共 27 行未计入 2,260 行主估算；主估算中的小型定义为前七项共 101 行。后续删除时应以实际 diff 统计为准。

其中 Python `ApiError` 与 `webui_new/static/app.js` 中仍在使用的 JavaScript `ApiError` 是不同定义；前端类应保留。删除未引用的位置校验辅助函数，也不代表删除运行器中实际使用的位置依据校验。

另有 29 个未使用导入候选，已经排除了 `__future__.annotations` 和包 `__init__.py` 的导出。例子包括 `engine.py` 的 `PolicyReport`、`REFUSAL`，`rag/pipeline.py` 的 `TextChunker`，ASR 路由的 `Request`、`AppError`、`request_id`。完整候选见 `tmp/code-audit-2026-10-04/static-scan.json`；需按导入项清理，不能连带删除仍使用的模块。

## 第二批：已被新架构替代，但需要连同装配代码整理

### 旧摘要仓储

`context/memory_repository.py:586` 起的 `_accumulate_segment()`、`claim_summary_range()`、`insert_session_summary()`、`get_session_summaries()` 仅由摘要测试调用。加上 `ClaimedSummaryRange`，约 224 行。

当前上下文来自 `BusinessServices.context()` 和原生会话/检查点消息，未看到摘要生成任务或摘要读取链路。`MEMORY_CONFIG["summary"]` 的配置也没有业务读取者。

建议退休这批 Python 方法和配置，并调整摘要专属测试。数据库中的历史摘要、`summary_watermark` 及迁移 SQL 保留，避免把源码精简变成数据迁移。

### 旧画像冲突确认仓储

`context/profile_repository.py` 共 435 行，保存画像事实版本和确认/拒绝请求。`MemoryService` 仍在构造它，`MemoryManager` 仍暴露该属性，但没有业务代码调用其事实读取、提议和确认方法；实际操作只出现在旧 stage-2 画像测试中。

当前个人资料走 `context/user_profile_repository.py`、`webui_new/routes/personal_profile.py` 及 `agent_runtime/user_profile.py`。可清理旧仓储以及无效装配属性，同时处理旧测试。

**保留 `context/profile_catalog.py`**：新的 `agent_runtime/user_profile.py` 仍使用其中的偏好校验和标准化方法。不能把这几个名称相近的文件一起删除。旧画像表及其迁移也不在本次源码清理范围内。

### 更小的兼容层

- `webui_new/quick_trip.py:52` 的 `inject_trip_entities()` 只被旧 quick-trip 单测使用；当前表单入口使用结构化输入。可在更新该测试后删除。
- `rag/document_loader.py` 中的旧 `load_text_documents()`、`iter_chunks()`，以及 `rag/chunker.py` 的字符切块兼容层，可随旧测试一起精简。**保留 `infer_category()` 和当前 `BlockChunker`**，它们有生产调用。
- `rag/schemas.py` 的旧 `KnowledgeChunk` 构造器未见生产使用，可随旧构造器测试整理；当前索引的 metadata 别名仍被兼容读取使用，不应顺带删除。
- `MemoryManager.get_recorded_response()` 及相应 service 方法当前只由集成测试调用，业务幂等恢复走 Supervisor 检查点。可作为后续 API 收敛项，先确认测试所覆盖的跨实例语义仍由现有路径保证。

## 配置和依赖中的残留

| 位置 | 现状 | 建议 |
| --- | --- | --- |
| `settings.py:151`、`.env.example:110` | 7 个 HyDE 配置键无人读取，但示例仍声称可以控制增强检索 | 与退休 HyDE 模块一起清理。 |
| `settings.py:290`、`.env.example:173` | `MEMORY_CONFIG["v2"]` 的迁移开关无业务读取者 | 移除失效开关及过时说明。 |
| `settings.py:295` | summary 开关和阈值无人读取 | 随旧摘要仓储清理。 |
| `settings.py:381`、`utils/preflight.py:348` | MCP 配置只做静态 readiness 检查，不实际建立客户端连接 | 随 MCP 包移除配置、检查项和相应观测标签。 |
| `settings.py:86` | `SYSTEM_CONFIG["enable_llm"]` 没有读取者，运行器仍构造模型 | 移除失效开关，或明确实现其行为，避免误导运维。 |
| `settings.py:80` | `AMAP_CONFIG.hotel_limit`、`mainland_only` 未被读取，当前服务自行约束查询 | 删除这两个无效配置项，不改变当前地区和数量限制。 |
| `settings.py:203` | `EVALUATION_CONFIG.context_messages` 未被读取 | 清理，或与真实评估上下文预算统一。 |
| `requirements.txt:40` | `ddgs==9.10.0` 无源码导入，安装环境中也没有其他包声明依赖它；天气/地图已经走高德 | 可移除这项直接依赖。 |
| `config.example.py` | 164 行重复配置参考；运行时只读 `settings.py`，README 引导配置 `.env.example` | 建议保留一份对外配置说明，合并或退休该重复参考文件。 |

依赖不能仅按项目中的 import 名判断：`mcp` 仍是 AgentScope 的依赖；`jsonschema` 由 `mcp`、`json_repair` 使用；`tqdm` 由 AgentScope 的 evaluate 模块使用。删除本项目的 MCP 客户端包，不意味着可以把这些依赖一起移除。`requests` 仍用于云端 embedding。

## 前端与仓库产物

正式页面的顶层 JS/CSS 文件都有引用，未发现可以整文件删除的顶层前端资源。明确未调用的内部函数有：

- `webui_new/static/app.js:1622`：`toggleHistorySearch()`，3 行。
- `webui_new/static/app.js:2752`：`setComposerContext()`，4 行。
- `webui_new/static/trip-intake-card.js:616`：`renderPlainText()`，7 行。

`webui_new/static/design-demos/` 有 **66 个文件，约 3.20 MiB**，不由正式页面加载，但部分 UI 检查和测试直接使用它们。建议归档到开发演示目录，并让对应检查显式加载演示资源。当前放在静态挂载内，也会被 `COPY . .` 带进生产镜像。

此外发现 7 组完全相同的静态资源副本，其中一组是正式头像与 demo 头像。只归档 demo 副本；保留正式页面引用的资源。

本地 `tmp/`、`.repro/`、`.codex-resume-work/` 合计约 **26.8 MiB**。它们没有被现有 `.dockerignore` 排除，也缺少对应的 `.gitignore` 目录规则；建议先完善忽略规则，减少误提交和镜像混入。它们可能包含诊断或恢复记录，本次未删除。

`.venv/` 约 459 MiB，已被 Git/Docker 忽略。它属于本地环境，占磁盘但不构成生产源码冗余；`env/`、`venv/`、`node_modules/` 当前为空。

## 增强检索入口需要明确产品处理

`chat.html` 仍展示增强检索选项，`app.js` 仍提交 `retrieval_mode="enhanced"`。但 `webui_new/manager.py:509` 明确把它标为 `effective_mode="standard"`、`status="fallback"`，说明当前引擎只暴露标准制度检索。

这是实际存在的入口与实现差异。精简时可以保留当前回退兼容，同时删除未接入的旧算法；若决定撤下入口，需要同步更新模板、JS、请求模型、展示元数据和路由契约测试。若后续需要增强检索，则应通过当前受控工具重新接入，而不是因为旧模块的单测通过就认为功能已经可用。

## 已排除的误判和应保留内容

- `RunStore._begin()`、`_check()`、`_stop()`、`_previous()`、`_apply()` 等通过 `call("方法名")` 和 `getattr(self, "_" + method)` 动态调用。普通名称计数会误报，已经核对主循环调用，必须保留。
- `rag/eval.py` 虽然不从 Web 入口到达，但它是检索质量评测工具，应保留；可以移到更明确的评测目录。
- `FileLongTermMemory`、`DisabledLongTermMemory` 仍被 `MemoryService` 的后端选择分支及测试使用，不能整文件删除 `long_term_memory.py`。
- FastAPI 的路由装饰器、Pydantic 校验器、抽象接口、包导出、字符串工具名不按普通函数引用计数删除。
- 旧展示数据恢复和 metadata 别名兼容仍有调用，保留其历史读取语义。
- `POST /login` 虽然当前前端已改用 `POST /auth/login`，但旧接口仍被注册，属于可被外部调用的兼容 API。仅凭仓库搜索不能证明没有旧客户端，应作为单独的接口退休项。
- 已执行的 SQL migrations 和现有数据库数据不因运行路径改变而删除。

## 抽样验证结果

命令在现有应用容器内运行，没有重启、重新部署或修改服务配置：

```text
docker exec hommey-app pytest -q
  tests/test_single_runtime.py tests/test_preference_storage_v2.py
  tests/test_state_simplification.py tests/test_native_context.py
  tests/test_user_profile_context.py tests/test_turn_context.py
  tests/test_tool_discovery.py tests/test_rag_hyde_mode.py
  tests/test_rag_phase4_evidence.py tests/test_memory_summaries.py
  tests/test_memory_stage2_profile.py tests/test_rag_phase0_eval.py --tb=short
```

结果：**103 passed，2 failed，3 skipped，20.64 秒**。

失败均发生在尚未做任何源码清理的现有基线：

1. `test_baseline_recall_floor_is_sane`：`recall_at_10` 为 0.0。黄金集期望 `01_travel_standards.txt` 等 6 个旧文件名，而当前 `data/documents` 中没有这些文件。评测又直接使用本地实际知识库目录，因此测试数据与期望不匹配。
2. `test_no_answer_queries_get_no_strong_evidence`：当前语料的检索结果未满足旧无答案阈值。需要使用固定评测语料重新核对与校准；本次尚未把此失败归因为纯粹的测试问题，也未据此判定线上答案有误。

建议先把评测语料固定到测试 fixture，并修正黄金集与文件身份的对应关系。不要通过删除这两个测试来制造绿色基线。抽样通过不代表完整测试套件通过，更不代表已经验证清理后的行为。

## 建议执行顺序

1. 清理未引用小函数、导入、旧 JSON 工具和 `ddgs`；不改业务流程。
2. 清理旧 autocommit 类、未接入 MCP 包和旧 RAG 模块；同步更新直接测旧实现的用例、配置及类型标注。
3. 整理旧摘要/画像仓储及兼容 API，归档 demo、补忽略规则；保持迁移和历史数据完整。
4. 每批运行对应回归，最后再运行完整测试。先解决当前评测基线失败，再用它判断后续清理是否产生回归。

本次产物：此报告及 `tmp/code-audit-2026-10-04/static-scan.json`。业务源码和用户已有改动保持原样。
