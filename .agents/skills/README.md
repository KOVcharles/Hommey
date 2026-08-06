# Hommey Agent Skills

本目录使用标准 Agent Skills 包结构。每个子目录至少包含一个带 YAML frontmatter 的 `SKILL.md`：

```text
<skill-name>/
├── SKILL.md       # 标准 name/description + 模型指令
├── hommey.yaml    # 可选 Hommey 运行时扩展
├── script/        # 可选 AgentScope 执行器
├── schemas/       # 可选输入输出契约
├── references/    # 可选按需参考资料
└── agents/        # 可选客户端 UI 元数据
```

通用 Agent Skills 客户端只需读取 `SKILL.md`。Hommey 额外读取 `hommey.yaml`，获得意图映射、Agent 入口、工具声明、风险、依赖和执行计划。不要在扩展文件中重复 `name` 或 `description`。

## 当前 Skill

| Skill | 用途 | Hommey 状态 |
| --- | --- | --- |
| `ask-question` | 基于企业 RAG 回答差旅制度 | 已接入 |
| `event-collection` | 收集并增量更新当前出差事项 | 已接入 |
| `plan-trip` | 组合事项、制度和外部信息生成行程 | 已接入 |
| `check-trip-compliance` | 依据制度证据检查拟定行程 | 已接入 |
| `query-info` | 查询差旅天气和公开交通信息 | 已接入 |
| `memory-query` | 查询当前用户的差旅记忆 | 已接入 |
| `preference` | 保存或更新差旅偏好 | 已接入 |
| `chitchat` | 处理简短礼貌交互和能力介绍 | 已接入 |
| `mcp-tool` | 路由已授权的 MCP 调用 | 默认停用 |

完整的加载、校验、编排、管理和测试说明见 [`docs/skill-system.md`](../../docs/skill-system.md)。
