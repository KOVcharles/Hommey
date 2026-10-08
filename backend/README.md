# Hommey 业务后端

Java 21、Spring Boot 3.5、Spring MVC、Sa-Token、Bean Validation、Spring JDBC、Flyway、PostgreSQL、Redis。内部执行凭证由 Spring Security 验证。

完整的开发、部署和回滚步骤见 [工程化指南](../docs/spring-python-engineering.md)。
用户登录、Redis 和多设备策略的配置见 [Sa-Token 配置与迁移指南](../docs/sa-token-auth.md)。

按业务能力组织包，包内采用 `Controller → Service → Repository`：

| 包 | 职责 |
| --- | --- |
| auth / security | 注册、登录、注销、多设备会话、用户归属、管理员权限、内部执行凭证 |
| profile | 个人资料校验、版本控制 |
| session | 会话、消息、附件绑定、删除与清空 |
| travel | 偏好、当前行程、行程记录 |
| agent | AI HTTP 客户端、流式转发、内部业务 API、事务提交 |
| common | 错误响应、请求 ID、JSON 与兼容 ID |
| web | 共享页面模板 |

Controller 只处理 HTTP、参数与身份；Service 定义事务和业务规则；Repository 通过参数化 SQL 操作数据库。使用 Spring 提供的 `JdbcClient`，保留既有 PostgreSQL 约束、JSONB 和 SQL 结构；不引入自研 ORM、通用 BaseController/BaseService 或额外分布式基础设施。

Web 应用运行在 MVC/Tomcat；`WebClient` 仅负责访问 Python 和转发 NDJSON。`@Transactional` 在业务服务层，个人资料采用乐观版本，Agent 变更使用版本检查与事务内幂等回执。业务写入失败时 Python 不能绕过接口直接写业务表。

```powershell
# 在 backend 目录，需 JAVA_HOME 指向 JDK 21，Docker 已启动
.\mvnw.cmd verify
.\mvnw.cmd spotless:apply
```

Linux / CI 使用 `bash mvnw verify`。测试通过 Testcontainers 启动独立 PostgreSQL 和 Redis，不复用应用数据库。

`db/migration/V1__legacy_schema.sql` 是既有 26 个迁移的**空库基线**。`V2` 增加业务回执和 AI 账号权限。已存在业务数据的数据库不能直接套用 V1；迁移前必须在备份副本演练。后续修改新增 V3、V4，不能修改已发布的迁移文件。
