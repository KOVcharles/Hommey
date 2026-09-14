# 项目结构

Hommey 是模块化单体：FastAPI + 一个 Supervisor 运行器 + 六类按需创建的专业角色。
没有单独的 Agent 服务、消息总线或 DAG 执行器。

| 目录/文件 | 职责 |
| --- | --- |
| runtime.py | 构造共享模型、MemoryManager、Supervisor 和附件服务 |
| agent_runtime/engine.py | 主/子工具循环、调度、恢复、终止 |
| agent_runtime/profiles.py | 六角色指令及工具/Skill 白名单 |
| agent_runtime/contracts.py、validation.py | 结构化契约、证据及写入校验 |
| agent_runtime/services.py | 受控业务查询和上下文构建 |
| agent_runtime/store.py | PostgreSQL 检查点与业务事务 |
| agent_runtime/render.py | 专业结果到前端文档的转换 |
| .agents/skills/ | 业务指南与展示元数据 |
| context/ | 用户消息、会话、偏好和行程存储 |
| core/integrations/ | 天气、地图、12306 等适配器 |
| core/presentation/ | 答案和行程补充卡片协议 |
| rag/ | 企业知识库检索、入库和刷新 |
| evaluation/ | 异步评估，不能影响业务结果或发起业务操作 |
| webui_new/ | HTTP、鉴权、NDJSON、管理页面 |
| webui_new/skill_platform/ | 只读 Skill 目录 |
| docker/ | 镜像与部署配置 |
| tests/ | 离线模拟、契约、数据库集成测试 |

旧 `agents/`、`core/orchestration/`、Skill 内 `script/agent.py` 已删除。
通用基础设施和历史数据表不构成备用业务流程。业务查询只能经 profiles 中允许的工具到达。
详细边界见 [架构与回滚说明](plans/2026-09-08-supervisor-implementation-and-rollback.md)。
