<p align="center">
  <img src="webui_new/static/brand/hommey-mark.svg" width="76" alt="Hommey 标志">
</p>

<h1 align="center">Hommey</h1>

<p align="center">
  <img src="https://img.shields.io/badge/Java-21-ED8B00?logo=openjdk&logoColor=white" alt="Java 21">
  <img src="https://img.shields.io/badge/Spring_Boot-3.5-6DB33F?logo=springboot&logoColor=white" alt="Spring Boot 3.5">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white" alt="Python 3.11">
  <img src="https://img.shields.io/badge/FastAPI-0.115.6-009688?logo=fastapi&logoColor=white" alt="FastAPI 0.115.6">
  <img src="https://img.shields.io/badge/PostgreSQL-16-336791?logo=postgresql&logoColor=white" alt="PostgreSQL 16">
  <img src="https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white" alt="Redis 7">
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
</p>

Hommey 是面向组内报销的专用 Agent，帮助组内成员查询报销制度、整理所需材料，并根据人员身份和经费项目确认适用条件。当前知识库以《重庆大学财务报销指南（2025 年版）》为依据，回答保留制度来源与页码。

## 主要能力

- 解答日常费用、差旅、版面费、会议和专家酬金等报销问题。
- 结合个人资料与常用经费项目，在条件缺失时通过对话或信息卡片补充。
- 检索制度原文，说明报销标准、材料要求和适用条件；差旅查询与规划作为辅助能力。

## 简要架构

本分支采用 Spring Boot 业务后端与 Python AI 服务。浏览器访问 Spring，用户、权限、资料、会话、偏好和行程由 Spring 管理；Python 接收限定用户/会话/请求的内部执行凭证，运行 Agent、RAG 与 AI 数据处理。详细说明见 [工程化开发、部署与回滚指南](docs/spring-python-engineering.md)和 [优化与发布说明](docs/spring-python-release-notes.md)。

- **业务后端**：`backend/` 使用标准 Controller / Service / Repository 分层、Spring Security、Bean Validation、Spring JDBC 和 Flyway。
- **AI 服务**：`ai_service/` 提供内部能力接口，通过业务 API 提交变更；不能直接写入业务表。
- **界面**：共享 `webui_new/` 页面和静态资源，由 Spring 提供入口并转发 NDJSON 流式回答。
- **Agent 运行时**：`agent_runtime/` 管理原生消息上下文、任务编排与工具调用，提示词和业务 Skill 分开维护。
- **制度检索**：`rag/` 对 CQU 文档切片，结合 pgvector 向量检索与 BM25 关键词检索；源文件位于 `data/documents/cqu/`。
- **数据存储**：PostgreSQL 保存账号、个人资料、会话记录与检索索引；Redis 维护短期会话状态和并发锁。

## 启动

安装 Python 依赖后生成独立开发配置，填写 `.env.engineering` 中的模型与 embedding key。新部署采用独立数据库和数据卷：

```powershell
python scripts/prepare_engineering.py
docker compose --env-file .env.engineering -f docker/docker-compose.engineering.yml up -d --build
```

启动后访问 <http://localhost:8088>。注册需要邮件配置和邀请码，VS Code 开发与新库邀请码生成步骤见 [工程化指南](docs/spring-python-engineering.md)。

原 Python 单体入口与 `docker/docker-compose.yml` 保留用于旧版本；不能将旧数据库卷直接接入新栈。

首次部署或更新制度文件后，需要通过管理员知识库页面刷新索引。

## 维护说明

- [CQU 知识库与更新方式](data/documents/README.md)
- [Agent 提示词维护](agent_runtime/prompts/README.md)
