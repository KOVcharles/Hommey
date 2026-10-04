# Hommey

Hommey 是面向组内报销的专用 Agent，帮助组内成员查询报销制度、整理所需材料，并根据人员身份和经费项目确认适用条件。当前知识库以《重庆大学财务报销指南（2025 年版）》为依据，回答保留制度来源与页码。

## 主要能力

- 解答日常费用、差旅、版面费、会议和专家酬金等报销问题。
- 结合个人资料与常用经费项目，在条件缺失时通过对话或信息卡片补充。
- 检索制度原文，说明报销标准、材料要求和适用条件；差旅查询与规划作为辅助能力。

## 简要架构

请求从 Web 界面进入 FastAPI，由主 Agent 管理对话并按需调用专门 Agent 与工具，最终返回回答、信息卡片和制度依据。

- **界面与 API**：`webui_new/` 提供聊天、资料管理和知识库入口，支持流式回答。
- **Agent 运行时**：`agent_runtime/` 管理原生消息上下文、任务编排与工具调用，提示词和业务 Skill 分开维护。
- **制度检索**：`rag/` 对 CQU 文档切片，结合 pgvector 向量检索与 BM25 关键词检索；源文件位于 `data/documents/cqu/`。
- **数据存储**：PostgreSQL 保存账号、个人资料、会话记录与检索索引；Redis 维护短期会话状态和并发锁。

## 启动

复制 `.env.example` 为 `.env`，填写模型、嵌入服务、数据库和鉴权配置。CQU 知识库范围使用 `HOMMEY_RAG_SEARCH_SCOPES=cqu/finance`。

```powershell
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
```

启动后访问 <http://localhost:8000>。注册需要邀请码，维护者可运行 `docker exec hommey-app python scripts/create_invites.py --count 5` 生成。

首次部署或更新制度文件后，需要通过管理员知识库页面刷新索引。

## 维护说明

- [CQU 知识库与更新方式](data/documents/README.md)
- [Agent 提示词维护](agent_runtime/prompts/README.md)
