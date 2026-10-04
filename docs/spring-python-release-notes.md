# Spring Boot + Python 改造、优化与发布说明

本次将业务后端迁移到 Spring Boot 3.5 / Java 21，Python 保留 Agent、模型调用、RAG 和 AI 数据处理。原有页面、接口路径、动画、资料填写、附件和对话交互继续使用。本轮发布前优化限于请求校验、取消请求的排错和开发配置提示，不重写 Agent 或数据库设计。

## 已完成的后端改造

| 范围 | 改动与结果 | 主要位置 |
| --- | --- | --- |
| 用户与权限 | Spring Security 验证公开 JWT，处理登录、注册、验证码、邀请码、管理员权限与用户归属 | `backend/.../auth`、`security` |
| 确定性业务 | Controller / Service / Repository 管理资料、会话、消息、偏好和行程，使用事务、版本检查和幂等回执 | `backend/.../profile`、`session`、`travel`、`agent` |
| AI 内部执行 | Spring 签发短期用户 / 会话 / 请求执行凭证；Python 使用内部业务 API 提交业务写入 | `ai_service`、`runtime.py` |
| 数据库与并发 | Flyway 管理结构，AI 使用受限数据库角色；保留 Redis 协调、取消栅栏和跨语言版本一致性 | `backend/src/main/resources/db/migration`、`ai_service/business.py` |
| 页面兼容 | 保留动态首页与登录页，补齐 Spring 模板属性；同步原版分类设置页、资料编辑、经费卡片和字体 | `PageController`、`webui_new/templates`、`webui_new/static` |
| Docker 与开发 | Nginx、Spring、Python Agent、RAG worker、PostgreSQL 和 Redis 独立部署；提供 Maven Wrapper 和 VS Code 调试配置 | `docker/docker-compose.engineering.yml`、`.vscode`、`scripts` |
| 构建与回归 | 固定 Python 依赖约束，增加 Java 集成测试、Python 服务边界测试及 GitHub Actions | `requirements.lock`、`backend/src/test`、`tests`、`.github/workflows/backend-ci.yml` |

这是一个共享 PostgreSQL、按表归属和账号权限划分职责的部署。Python 不直接写业务表，租户归属仍由应用接口与查询条件控制。完整部署、鉴权与事务约定见 [工程化指南](spring-python-engineering.md)。

## 本轮小幅优化

### 请求编号校验复用已编译的规则

`RequestIdFilter.java` 原来每次调用 `String.matches()` 都会重新编译正则；现在使用静态 `Pattern`，每次请求只创建 matcher。接受的字符、长度、非法编号的替换方式以及响应头保持一致。这消除了重复编译，不改变请求协议或安全约束。

### 取消 Agent 请求失败时保留诊断线索

`AgentClient.java` 原来将取消回调的错误直接吞掉。现在记录 `agent_cancel_failed`、请求编号和异常类型，便于检查浏览器停止生成或断连后取消没有送达的情况。取消仍有 5 秒超时，失败不阻塞业务响应；日志不记录执行 token、请求内容或原始异常正文。

### 开发配置提示区分业务准备与 AI 就绪

`scripts/prepare_engineering.py` 继续保留已有 `.env.engineering`、数据库密码和 RSA 密钥。执行结束后检查模型 key 和云 embedding key 是否配置，只打印缺少的变量名，并提醒初始化知识库及检查 `/readyz`。

这解决了“容器和业务页面已启动，但 AI 没有配置好”的误解。进程存活使用 `/healthz`，完整 AI 依赖就绪使用 `/readyz`；已有就绪检查不需要改动。

## 配置与知识库问题的修复

- 新栈的运行配置已接入原版使用的文本模型和 embedding 服务，模型名称、服务地址和向量维度一并匹配；敏感配置仅在被 Git 忽略的本地文件中保存。
- 独立的新数据库已为《重庆大学财务报销指南（2025 年版）》建立索引：22 个物理页面、59 个检索片段。
- `HommeyWebInstance.initialize()` 在模型 key 缺失时返回 `LLM_NOT_CONFIGURED` 和明确的配置提示；流式与非流式响应都保留请求编号，流式错误声明不可通过直接重试解决。接口不暴露 SDK 的原始配置错误。
- 实际浏览器已用普通测试账号完成“酒水报销怎么做？”的模型调用、制度检索及回答返回。本项验证确认技术链路可用，不代替制度回答质量的专项评测。

## 本机目录切换

日常开发目录改为 `D:\Hommey`，分支为 `codex/spring-python-backend`。在 VS Code 打开该目录即可继续编辑。

迁移时采取以下步骤：

1. 将旧目录中未提交的文件、二进制补丁和分支信息保存到 `D:\Hommey-backups\before-spring-<时间戳>`，并创建包含未跟踪文件的 Git stash。
2. 备份旧 `.env`，使用新架构的 `.env.engineering`；复制原新栈的 RSA 密钥，保持已有登录凭证可用。原 Python 虚拟环境继续保留在 D 盘。
3. 将 D 盘主工作目录切换到改造分支；归档 C 盘临时工作树。保留需要的本地诊断和截图文件。
4. 从 D 盘重新构建和启动 `hommey-engineering`，更新容器的配置挂载路径。沿用同名新栈数据库和数据卷，不清空用户、会话或知识库。
5. 验证普通账号登录、业务 API、AI readiness 和跨语言联调。旧服务与旧数据库不自动迁移、删除或启动。

旧未提交文件的副本和恢复说明位于备份目录；在单独的旧版本 checkout 中恢复补丁和未跟踪文件，避免把整份旧 stash 直接覆盖到新分支。Git 历史中的旧版本可随时检出。

## 验证记录

| 检查 | 结果与范围 |
| --- | --- |
| Java 21 / Maven | `spotless:apply verify`；13 项 Testcontainers 集成测试通过，使用独立 PostgreSQL 与 Redis |
| Python 3.11 / Linux | 与 PR CI 相同的回归范围：204 项通过、1 项需要单独数据库的测试跳过 |
| 格式 | Java Spotless；新 Python 服务、开发脚本及边界测试的 Black 检查通过 |
| 新栈联调 | 两项真实 Spring / PostgreSQL / Python 联调：Supervisor 业务提交与幂等、附件上传解析下载与消息绑定；测试模型不调用外部模型 |
| 页面与真实模型 | 动态首页、资料和经费保存、手机设置布局、键盘焦点、普通账号登录及制度问答已在浏览器验证 |
| AI readiness | 模型配置、embedding、数据库、Redis、活动检索索引和 RAG worker 检查通过；真实模型连通性已验证 |
| Git 与敏感文件 | 本地 `.env*`、私钥、测试账号凭证、运行数据和诊断文件不进入 PR |

CI 的完整测试文件列表见 `.github/workflows/backend-ci.yml`。上述检查只使用独立开发库和自建测试数据，不操作旧用户数据。

## 发布范围与运行说明

本次发布为推送改造分支和创建 PR；本机使用新版本。入口为 `http://localhost:8088`，原有本地测试账号继续使用。新机器需自行生成开发配置并填写模型、embedding 与邮件服务参数；仓库不携带本机配置。

PR 不自动切换生产服务器。现有 `deploy.yml` 在 `main` 更新时仍启动旧 Compose；正式切换到 Spring 栈前，需要按工程化指南完成生产环境配置、旧业务数据迁移、邮件与邀请码验收、入口及回滚方案，再调整服务器部署流程。

恢复开发部署时使用同一个工程化 Compose 文件，避免将旧数据库卷接入新栈。停止新栈可执行 `docker compose --env-file .env.engineering -f docker/docker-compose.engineering.yml down`；保留数据卷，不添加 `-v`。
