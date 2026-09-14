# Hommey Skill 系统

Skill 是主 Agent 与专业子 Agent 按需读取的业务指南。执行逻辑只有 `agent_runtime` 一套；
Skill 不注册 Python 执行器，不生成 DAG，也不能授予新工具权限。

## 包结构

```text
.agents/skills/<name>/
├── SKILL.md       # name、description frontmatter 和业务正文
└── hommey.yaml    # 可选：version、display_name、category、domain、risk_level、catalog_order
```

`HOMMEY_SKILLS_ROOT` 可指定指南目录，默认路径相对项目根目录解析。
`utils/skill_loader.py` 发现并读取文档，`core/skill_definition.py` 校验元数据；
未支持的扩展字段会报错，避免旧 execution/entrypoint 配置看似有效却没有执行。

## 六个专业角色

| 子 Agent | 可读 Skill |
| --- | --- |
| trip_context | event-collection |
| policy_rag | ask-question |
| memory | memory-query、preference |
| travel_info | query-info、train-query、place-query |
| trip_planner | plan-trip |
| compliance | check-trip-compliance |

角色的实际权限由 `agent_runtime/profiles.py` 定义。主 Agent 可以读取以上所有指南；
子 Agent 仅能读取本角色指南，使用本角色业务工具，最后通过 `report` 返回结构化摘要。
`evaluate-turn` 仅用于独立的事后评估，不属于用户意图或主 Agent 可调度角色。

## 指南、数据和执行的边界

- Skill 说明如何检索、总结、引用证据和报告缺项，不能嵌入公司金额作为事实来源。
- 公司标准和审批规定从内部 RAG 查询；个人记录由 memory 按已鉴权用户检索。
- 输入契约、输出报告与写入验证在 `agent_runtime` 中，模型不能用文档绕过校验。
- 主 Agent 根据已完成结果决定下一步；独立查询可以并发，有依赖的任务显式传结果 ID。
- 管理员页面只展示指南和关联角色。变更通过代码审阅发布，没有在线启停或任意脚本上传。

## 代码位置

- `agent_runtime/engine.py`：统一模型工具循环和委派。
- `agent_runtime/services.py`：RAG、记忆、天气、交通、酒店业务工具。
- `core/integrations/trains.py`：12306 适配器。
- `evaluation/judge.py`：离线评估器。
- `scripts/init_knowledge_base.py`：知识库初始化工具。

完整执行和回滚边界见 [当前架构说明](plans/2026-09-08-supervisor-implementation-and-rollback.md)。
