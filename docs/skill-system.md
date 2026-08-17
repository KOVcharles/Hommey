# Hommey Skill 沉淀与运行机制

本文档描述当前项目如何把一次性的 Prompt、Agent 代码和业务编排沉淀为可发现、可校验、可组合、可启停和可观测的运行时 Skill。内容以仓库当前实现为准。

## 1. Skill 在项目中的含义

Hommey 的 Skill 是一个受 Git 管理的业务能力包。一个完整 Skill 通常包含：

1. 稳定的业务流程和模型约束；
2. 机器可读的元数据与执行计划；
3. 可动态加载的 AgentScope 执行器；
4. 可选的输入输出 Schema、参考规则和辅助脚本；
5. 对应的触发、边界、无证据和正常路径测试。

这里的“沉淀”是工程化、评审式沉淀，不是模型自动学习：

- 系统不会从成功对话中自动创建 Skill；
- 执行轨迹不会自动修改 `SKILL.md`、`hommey.yaml` 或 Agent 代码；
- 新能力和规则变更仍需开发者修改文件、运行测试、经过 Git 评审并部署；
- 管理页面目前只负责查看、启停和观察，不负责在线编辑或发布。

同时，Skill 与其他数据层保持分离：

| 内容 | 存放位置 | 原因 |
| --- | --- | --- |
| 稳定的处理流程、拒答规则、输出要求 | Skill | 可复用、可版本化 |
| 公司补贴金额、住宿上限、审批规定 | RAG 文档 | 企业制度会变化，必须基于证据 |
| 用户偏好、历史行程、当前出差任务 | [记忆系统](memory-system.md) | 按用户隔离、动态变化 |
| Skill 启停和执行轨迹 | PostgreSQL | 运行时管理与观察 |

## 2. Skill 包结构

默认根目录由 `HOMMEY_SKILLS_ROOT` 控制，未配置时为 `.agents/skills`。

```text
.agents/skills/<skill-name>/
├── SKILL.md               # 标准入口：frontmatter 元数据 + 模型指令，必需
├── hommey.yaml            # Hommey 运行时、治理和编排扩展，可选
├── script/
│   └── agent.py           # AgentScope 执行器，有 agent_name 时必需
├── schemas/               # 可选输入输出 JSON Schema
├── references/            # 可选参考规则
└── agents/openai.yaml     # 可选 UI/生态元数据，当前核心运行时不读取
```

### 2.1 SKILL.md

`SKILL.md` 遵循 Agent Skills 的通用结构，由 YAML frontmatter 和 Markdown 正文组成：

```markdown
---
name: plan-trip
description: Build a company business-trip itinerary. Use for route, lodging, schedule, budget, and reimbursement-preparation advice.
---

# 规划合规公司差旅

1. 收集结构化出差事项。
2. 查询制度和外部信息。
3. 生成行程并检查合规性。
```

frontmatter 中的 `name` 和 `description` 是标准发现元数据：应用启动时只需读取它们即可构建能力目录；Skill 被调用后才向 Agent 提供 Markdown 正文。`name` 必须与目录名一致，`description` 应同时说明能力和触发场景。

正文回答“具体应该怎么做”，通常沉淀：

- 分步骤工作流；
- 可使用和不可使用的数据来源；
- 证据要求、拒答边界和禁止事项；
- 输出格式和质量标准；
- 必要的业务示例。

`SkillLoader.get_skill_content()` 只返回 Markdown 正文，不把 frontmatter 重复注入模型 Prompt。

### 2.2 hommey.yaml

`hommey.yaml` 是可选的平台扩展，只回答“如何在 Hommey 中运行和治理”。它保存：

- 版本、展示名、分类、领域和意图顺序；
- Agent 名称和 Python 入口；
- 声明工具、风险等级和默认启停状态；
- Skill 依赖与按优先级编排的执行步骤；
- 输入输出 Schema 路径。

标准 Agent Skills 客户端可以忽略该文件，继续依据 `SKILL.md` 发现和使用能力。纯标准 Skill 可以不提供 `hommey.yaml`；它会被通用加载器发现，但不会进入 Hommey 意图目录、Agent 注册表、管理页或调度计划。只有需要接入这些平台能力的 Skill 才需要扩展文件。`hommey.yaml` 禁止重复定义 `name` 和 `description`，从结构上避免双份元数据漂移。

### 2.3 script/agent.py

执行器负责把协调器传入的结构化上下文转为实际动作，例如：

- 调用 LLM；
- 查询 RAG；
- 查询天气或公开交通信息；
- 读取用户记忆；
- 调用 MCP 工具；
- 规范化并返回 JSON 结果。

执行器必须包含 `AgentBase` 子类。运行时第一次使用时动态导入并实例化，后续从当前用户实例的 Agent 缓存复用。

## 3. Skill 契约

`core/skill_definition.py` 使用 Pydantic 分别校验标准 frontmatter 和 Hommey 扩展，再合并成运行时 `SkillDefinition`。

### 3.1 标准 frontmatter

| 字段 | 约束与用途 |
| --- | --- |
| `name` | 必需；小写字母、数字和连字符，最长 64 个字符，必须与目录名一致 |
| `description` | 必需；最长 1024 个字符，同时描述能力和触发场景 |
| `license` | 可选；许可证名称或文件引用 |
| `compatibility` | 可选；运行环境要求 |
| `metadata` | 可选；扩展键值元数据 |
| `allowed-tools` | 可选；实验性的预授权工具声明 |

项目自己的 Skill 只写 `name` 和 `description`，其余字段由加载器兼容但不要求。

### 3.2 Hommey 扩展字段

| 字段 | 约束与用途 |
| --- | --- |
| `version` | `x.y.z` 三段数字版本 |
| `display_name` | 管理页面和用户界面的展示名 |
| `category` | `business`、`workflow`、`capability`、`interaction` |
| `domain` | 默认 `business-travel` |
| `intent` | 触发意图；配置后必须同时配置 `agent_name` 和 `execution` |
| `agent_name` | 兼容运行时使用的 Agent 名 |
| `entrypoint` | 默认 `script/agent.py` |
| `user_facing` | 是否声明为用户直接能力，默认 `true` |
| `enabled_by_default` | 数据库没有覆盖配置时的启停默认值 |
| `risk_level` | `low`、`medium`、`high` |
| `catalog_order` | 意图 Prompt 和管理列表的排序 |
| `tools` | 声明所需工具类型 |
| `requires` | Skill 依赖及依赖目的 |
| `execution` | 实际 Agent 步骤、优先级、原因和期望输出 |
| `input_schema` / `output_schema` | Schema 相对路径 |

允许的工具标识固定为：

```text
active_trip_context
rag_retrieval
travel_information
weather
web_search
memory
mcp
```

加载时还会校验：

- Skill 目录必须存在带合法 frontmatter 和非空正文的 `SKILL.md`；
- `hommey.yaml` 存在时必须符合 Hommey 扩展契约；
- 有 `agent_name` 时入口文件必须存在；
- 声明的 Schema 文件必须存在；
- 任意 Skill 错误在严格加载模式下会汇总并让启动尽早失败。

## 4. 发现与加载流程

### 4.1 SkillLoader

`SkillLoader` 从项目根目录解析 Skill 路径，不依赖进程当前工作目录。发现过程如下：

```text
遍历 Skill 根目录的所有子目录
  → 查找并解析 SKILL.md frontmatter
  → 校验 name、description、目录名和正文
  → 按需读取 hommey.yaml
  → 校验入口和 Schema 文件
  → 合并为 name → SkillDefinition 映射
```

机器发现以标准 `SKILL.md` 为准；Hommey 扩展只增强运行能力，不接管通用发现语义。为兼容迁移前的内部调用，`load_manifests()` / `get_manifest()` 暂时保留为别名，新代码统一使用 `load_definitions()` / `get_definition()`。

### 4.2 LazyAgentRegistry

运行时初始化时只建立映射，不立即导入所有 Agent：

```text
skill name ──→ entrypoint 路径
agent_name ──→ skill name
```

第一次访问某个 Agent 时：

1. 根据 Skill 名或兼容 Agent 名找到入口；
2. 通过 `importlib` 动态导入模块；
3. 查找模块中的 `AgentBase` 子类；
4. 注入 `name` 和共享 `model`；
5. 如果构造函数声明了 `memory_manager` 或 `mcp_manager`，按需注入；
6. 将实例缓存到当前 `HommeyWebInstance` 或 CLI 会话。

因此，启动时不需要加载全部业务 Agent，未触发的能力不会产生实例化成本。Skill 名和旧 Agent 名都可以用于注册表查找，例如 `ask-question` 与 `rag_knowledge` 会映射到同一入口。

## 5. 从用户意图到 Skill

### 5.1 意图目录

`core/intent_catalog.py` 启动时读取所有 `SkillDefinition`，并按 `catalog_order + name` 排序生成：

- `intent → skill`；
- `skill → intent`；
- 意图展示名；
- 提供给意图识别 LLM 的能力列表。

`unclear` 和 `unsupported` 是仅有的非 Skill 意图，它们不会生成执行计划。

### 5.2 路由与门禁

请求先经过规则路由和领域门禁，复杂或有上下文的请求再进入 LLM 意图识别：

```text
用户输入
  → 空输入/乱码/领域外/禁止操作门禁
  → 高置信度规则路由（能命中时）
  → LLM 多意图识别（需要上下文时）
  → 逐意图置信度门槛
  → 语义任务分解与授权校验
  → hommey.yaml execution 编译可信 DAG
```

默认 Skill 置信度门槛是 `0.65`；外部信息查询因涉及联网，门槛是 `0.75`，并额外要求明确查询对象（天气、车票、航班、酒店等公共信息无需完整差旅上下文）。

门禁还会阻止：

- 私人旅游和公司差旅无关请求；
- 编程、作业、投资等领域外问题；
- 预订、付款、转账、审批和报销提交；
- 信息不足、只有标点或过短的输入。

### 5.3 声明式编排

`core/orchestration/graph_builder.py` 从每个 Hommey 扩展的 `execution` 编译执行节点。意图识别层不选择 Agent，也不生成执行顺序。

多意图时，validator 先确保每个语义任务都来自已授权意图；graph builder 再展开 Skill 工作流，并在 DAG 层折叠被工作流覆盖的独立节点。行程规划中的制度与外部信息查询因此仍位于事项收集之后。

## 6. 编排与执行协议

### 6.1 优先级语义

协调器按优先级分组：

- 不同优先级顺序执行；
- 同一优先级使用 `asyncio.gather()` 并行执行；
- 后一优先级可以读取所有前序结果；
- 同一并行批次中的 Agent 不能读取彼此尚未完成的结果。

### 6.2 Agent 输入

协调器统一向 Agent 发送 JSON：

```json
{
  "context": {
    "reasoning": "意图识别原因",
    "intents": [],
    "key_entities": {},
    "rewritten_query": "标准化后的用户问题",
    "recent_dialogue": [],
    "user_preferences": {},
    "active_trip": null
  },
  "reason": "本步骤为什么执行",
  "expected_output": "期望输出",
  "previous_results": [],
  "task_params": {}
}
```

`task_params` 只在调度步骤提供额外参数时出现，当前主要为 MCP 工具调用预留。

### 6.3 Agent 输出

Agent 返回 `Msg`。如果 `content` 是 JSON 字符串，协调器解析为对象；不是 JSON 时包装为：

```json
{"output": "原始文本"}
```

协调器为每步结果补充：

- `status`；
- `agent_name`；
- `duration_sec`；
- `data`；
- 错误时的 `message`。

单个 Agent 异常会被转为结构化错误，不直接中断同批其他 Agent。Web 层随后检查聚合结果，严重的 Agent 错误会转换为统一上游错误响应。

## 7. 组合 Skill：plan-trip 示例

`plan-trip` 是目前最完整的工作流型 Skill：

```text
Priority 1  event-collection
Priority 2  ask-question + query-info + train-query（并行）
Priority 3  plan-trip
Priority 4  check-trip-compliance
```

具体行为：

1. `event-collection` 从本轮输入、偏好和当前出差任务提取结构化事项；
2. 如果缺少出发地、目的地、出发日期、出差目的，以及时长或返程日期，协调器暂停后续步骤；
3. 收集完整后，`ask-question` 查询公司制度，`query-info` 查询天气和路线级公开信息，`train-query` 查询真实车次/时刻/余票（结果经 `all_info` 聚合，plan-trip 直接引用真实车次，不虚构）；
4. `plan-trip` 读取前序结果生成工作优先的行程；
5. `check-trip-compliance` 只依据 RAG 证据进行合规检查，无证据时返回 `unknown`。

若用户上一轮只触发事项收集，本轮补齐最后一个字段，协调器会读取 `plan-trip` 的 Hommey 扩展步骤自动续跑，不要求用户再输入一次“生成行程”。

## 8. SKILL.md 如何进入执行

多个执行器会在每次调用时通过 `SkillLoader.get_skill_content()` 动态读取自己的 `SKILL.md`，并作为 Prompt 的“Skill 指令”部分，例如：

- `ask-question`；
- `check-trip-compliance`；
- `plan-trip`；
- `query-info`；
- `memory-query`；
- `preference`。

这使上述 Skill 的流程文案修改后可在下一次调用生效，无需重新实例化 Agent。

当前并非所有 Agent 都把 `SKILL.md` 注入 Prompt：`event-collection`、`chitchat` 和 `mcp-tool` 的主要规则仍直接写在执行器代码中。`references/` 不会自动注入 Prompt；执行器必须通过受包目录约束的 `SkillLoader.get_skill_resource()` 按需读取。例如合规检查会显式加载 `references/evidence-rules.md`。

## 9. 启停与管理平台

### 9.1 配置存储

PostgreSQL 的 `skill_settings` 保存：

```text
skill_name
enabled
config_overrides
updated_by
updated_at
```

如果不存在覆盖记录，则使用 Hommey 扩展的 `enabled_by_default`。当前只有 `mcp-tool` 默认关闭。

协调器执行前按“Agent 所属 Skill”过滤调度步骤。工作流里的每个步骤分别检查其实际 Skill，因此停用 `ask-question` 会移除 `rag_knowledge` 步骤。

当 PostgreSQL 未配置时，系统使用 Hommey 扩展默认值，管理 API 不允许修改。读取启停状态发生数据库异常时，当前实现会回退到默认值，即 fail-open 到扩展配置的默认状态。

### 9.2 管理页面

管理员页面位于：

```text
/admin/skills
```

API：

```text
GET   /api/admin/skills
GET   /api/admin/skills/{skill_name}
PATCH /api/admin/skills/{skill_name}/enabled
```

只有 `role=admin` 的 JWT 用户可以访问。页面展示：

- Skill 列表、版本、分类和启停状态；
- 来自 `SKILL.md` 的描述和正文，以及来自 `hommey.yaml` 的风险与工具声明；
- `requires` 生成的依赖图；
- 最近执行记录、运行次数、成功率和平均耗时。

`config_overrides` 字段目前仅预留，管理 API 和执行器尚未消费它。

## 10. 执行轨迹与可观测性

PostgreSQL 的 `skill_execution_runs` 为每个执行步骤记录：

- Skill 名称和 Hommey 扩展版本；
- 同一次协调请求的 `request_id`；
- 用户 ID；
- 状态、耗时和错误码；
- 意图与结构化实体摘要；
- Agent 名和输出状态摘要；
- RAG 来源或证据数量；
- 可选的父运行 ID。

隐私策略是默认不保存完整用户问题、完整回答、完整 Prompt、RAG 原文或凭据。执行记录失败会被吞掉，不影响主业务请求。

管理页统计基于最近读取的最多 500 条全局运行记录，而不是全量历史；单个 Skill 详情最多展示最近 20 条。

当前轨迹只用于观察，不会自动形成训练数据、自动更新 Prompt 或生成新版本。

## 11. 当前 Skill 清单

| Skill | 触发意图 | 类型 | 风险 | 默认状态 | 主要能力 |
| --- | --- | --- | --- | --- | --- |
| `ask-question` | `rag_knowledge` | business | medium | 开启 | 基于企业 RAG 回答差旅制度 |
| `event-collection` | `event_collection` | business | low | 开启 | 收集并增量更新当前出差事项 |
| `plan-trip` | `itinerary_planning` | workflow | medium | 开启 | 组合事项、制度、外部信息和合规检查 |
| `check-trip-compliance` | `trip_compliance` | business | high | 开启 | 基于制度证据检查行程合规性 |
| `query-info` | `information_query` | capability | medium | 开启 | 天气和公开交通信息（无需差旅上下文） |
| `train-query` | `train_query` | capability | medium | 开启 | 12306 车票/车次/时刻/历时/余票，结构化喂给编排 |
| `memory-query` | `memory_query` | capability | medium | 开启 | 查询当前用户差旅记忆 |
| `preference` | `preference` | capability | medium | 开启 | 识别并保存差旅偏好 |
| `chitchat` | `chitchat` | interaction | low | 开启 | 简短问候、感谢和能力引导 |
| `mcp-tool` | `mcp_tool` | capability | high | 关闭 | 调用受配置的 MCP 工具 |

`user_facing=false` 当前是声明和展示信息，并不会把对应意图排除出意图目录；是否可触发主要仍由领域门禁、置信度和启停状态控制。

## 12. 版本与部署生命周期

### 12.1 什么变更需要重启

| 变更 | 当前生效方式 |
| --- | --- |
| `SKILL.md` 正文 | 对动态读取该文件的 Agent，通常下一次调用生效 |
| `SKILL.md` frontmatter | 能力目录在模块导入时生成，通常需要重启应用 |
| `hommey.yaml` | 意图目录和调度规则在模块导入时生成，通常需要重启应用 |
| `script/agent.py` | 已加载 Agent 有实例缓存，需要重建用户实例或重启应用 |
| Schema / references | 取决于执行器是否实际读取；仅改文件不保证运行时生效 |
| 管理页启停 | 写入 PostgreSQL 后下一次调度生效 |

开发 Compose 已挂载源码，修改后通常只需：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml restart hommey
```

非开发镜像需要重新构建。

### 12.2 版本规则

`hommey.yaml` 强制三段数字版本，但当前没有自动版本比较、发布注册表、回滚包或兼容性迁移。版本号需要开发者在行为或契约变化时主动更新，并与代码一起通过 Git 发布。

## 13. 新增或演进 Skill 的标准流程

### 13.1 明确沉淀边界

先判断内容应属于哪里：

- 稳定、跨用户复用的处理方法 → Skill；
- 公司具体政策和数值 → RAG 文档；
- 用户个人事实 → 记忆系统；
- 单次临时任务 → 不应直接沉淀为公共 Skill。

### 13.2 创建包

1. 在 `.agents/skills/<name>/` 创建带 `name`、`description` frontmatter 的 `SKILL.md`；
2. 若需要执行器，添加 `script/agent.py` 和唯一明确的 `AgentBase` 子类；
3. 需要接入 Hommey 运行时时，再添加 `hommey.yaml`，声明意图、Agent、入口、风险、工具和执行计划；
4. 需要结构契约时添加 `schemas/input.json`、`schemas/output.json`；
5. 需要证据规则时添加 `references/`，并在执行器中显式读取；
6. 为正常、触发、低置信度、领域外、无证据、异常和组合路径增加测试；
7. 更新版本号和相关文档；
8. 运行测试，经过 Git 评审后部署并重启应用。

### 13.3 建议的最小标准 Skill

```markdown
---
name: example-skill
description: Handle a specific company business-travel task. Use when the user asks for the corresponding workflow.
---

# Example Skill

Follow the company travel workflow and return a structured result.
```

不需要 Hommey 执行器、意图路由和管理能力时，到这里已经是一个可移植的标准 Skill。

### 13.4 可选的 Hommey 扩展

```yaml
version: 1.0.0
display_name: 示例能力
category: business
intent: example_intent
agent_name: example_agent
entrypoint: script/agent.py
user_facing: true
enabled_by_default: true
risk_level: low
catalog_order: 50
tools: []
execution:
  - skill: example-skill
    agent_name: example_agent
    priority: 1
    reason: 执行示例任务
    expected_output: 结构化示例结果
```

当前仓库内置 Skill 都接入了意图目录，因此都提供 `hommey.yaml` 和 `intent`。加载器本身也支持只有 `SKILL.md` 的可移植 Skill；这类 Skill 可以被发现，但不会出现在 Hommey 管理页，也不会自动生成 Agent 调度步骤。

## 14. 当前实现边界

维护和扩展时需要注意：

1. `tools` 目前是白名单枚举和展示元数据，不是运行时沙箱；执行器 Python 代码仍在主进程内执行。
2. `risk_level` 和 `user_facing` 尚未自动转换为审批、权限或路由策略。
3. `requires` 用于依赖图和设计表达；实际执行以 `execution` 为准，没有统一的依赖存在性、版本范围或级联启停校验。
4. JSON Schema 当前只校验文件存在，不会由协调器统一验证 Agent 的实际输入输出；少数 Agent 使用自己的 Pydantic 模型补充校验。
5. `references/` 没有统一渐进加载机制，必须由 Agent 代码显式读取。
6. Hommey 扩展没有跨 Skill 校验重复 intent、重复 agent_name、依赖循环或执行步骤引用是否存在。
7. 停用工作流主 Skill 只会移除归属于该 Skill 的 Agent 步骤，不会自动停用或跳过其依赖步骤。
8. 启停状态读取失败时回退默认值，高风险 Skill 若默认开启会形成 fail-open 行为。
9. 动态执行器不是安全插件沙箱；只有可信、经过代码评审的 Skill 才应进入运行目录。
10. 聚合结果当前总是初始化为 `completed`，函数中用于改成 `partial_failure` 的代码位于提前返回之后，实际上不可达。
11. 运行记录的 `parent_run_id`、`config_overrides` 已预留但尚未形成完整能力。
12. 没有自动创建、在线编辑、在线发布、灰度、回滚或基于轨迹自动优化 Skill 的闭环。

## 15. 测试与检查

核心契约测试：

```bash
PYTHONPATH=. pytest -q \
  tests/test_skill_platform.py \
  tests/test_skill_registry.py \
  tests/test_intent_catalog.py \
  tests/test_multi_intent_routing.py \
  tests/test_orchestration.py
```

快速检查所有标准 Skill 和 Hommey 扩展：

```bash
PYTHONPATH=. python -c "from utils.skill_loader import SkillLoader; print(sorted(SkillLoader().load_skills()))"
```

管理员可在 `/admin/skills` 核对：

- `SKILL.md` 是否被发现且 frontmatter 合法；
- `hommey.yaml` 是否正确合并；
- 版本、风险、工具和依赖是否正确；
- 默认/覆盖启停状态；
- 最近执行状态、耗时和证据数量。
