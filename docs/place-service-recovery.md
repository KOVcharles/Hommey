# 地点查询恢复说明（2026-10-05）

## 问题与原因

在“确认会议 / 办公地点”里输入“上海大学”后，界面提示“地点查询暂时不可用，请修改关键词重试”。容器与业务后端仍在运行，失败发生在地点能力链路。

1. 工程化栈使用独立的 `.env.engineering`，其中 `HOMMEY_AMAP_WEB_KEY` 为空。旧目录的配置已经备份，但地图 Key 未补入新配置。Python 返回 `PLACE_SERVICE_NOT_CONFIGURED`；容器健康检查通过不表示每一个可选外部能力都已经配置。
2. Spring 的能力代理从 Servlet 取得已经编码的路径与查询参数，再交给 WebClient 的字符串 URI 方法。中文参数中的 `%E4...` 被再次编码为 `%25E4...`，Python 收到的城市和关键词因此不是原来的中文。即使补上 Key，这种转发也会影响地点匹配。
3. 地点选择组件把不同错误都替换成“修改关键词重试”，掩盖了缺配置与服务失败的真实原因。

## 改动

| 文件或配置 | 改动与作用 |
| --- | --- |
| `backend/src/main/java/com/hommey/backend/agent/AgentClient.java` | 为已经编码的能力请求使用 `DefaultUriBuilderFactory.EncodingMode.NONE` 并生成 URI，再通过 WebClient 的 URI 方法发送；普通转发与 multipart 转发均保留原查询参数及配置中的基础路径。聊天流和业务 JSON 请求沿用既有方式。 |
| `backend/src/test/java/com/hommey/backend/agent/AgentClientTest.java` | 两项测试向实际本地 HTTP 服务发请求，验证中文、空格、`&`、`+`、`%`、`#` 不会被重复编码，基础路径、multipart 正文及认证头正确。 |
| `webui_new/static/trip-choices.js` | 展示 API 已提供的业务错误；普通网络失败提示稍后重试，不再一律要求修改关键词。 |
| `.github/workflows/backend-ci.yml` | 加入地图相关 Python 回归、地点选择组件的 JavaScript 语法检查；Java 新测试由 Maven verify 自动运行。 |
| 本机 `.env.engineering`（不提交） | 仅从用户原目录的本地配置备份补回缺失的高德 Key，保留其他配置。密钥不进入 Git、镜像层或说明文档。 |

这次没有修改数据库、用户权限或地点归属校验，也没有绕过鉴权。底图仍由服务器请求并转发，前端不会得到高德 Key。

## 部署与配置

新环境需在 `.env.engineering` 配置 `HOMMEY_AMAP_WEB_KEY`，使用可调用高德 Web 服务的 Key。该变量已由 Compose 传入 Python 服务。只编辑宿主机配置文件不会更新已有容器，需要重建或重建容器：

```powershell
docker compose --env-file .env.engineering -f docker/docker-compose.engineering.yml up -d --build --wait
```

本机已从 `D:\Hommey` 完成此操作。更新后刷新 `http://localhost:8088/`，重新输入城市和地点关键词；同名大学有多个校区，应按名称与地址选择实际会议或办公地点。

## 验证

- Docker 内 Java 21：新增两项真实 HTTP 转发测试通过，执行 Spotless 格式化后编译、测试成功。
- Linux / Python 3.11：地点信息、地图集成、出行选择回归 32 项通过，1 项需显式启用的联调测试跳过。
- 8088 → Spring → Python → 高德真实链路：搜索上海市的“上海大学”返回 HTTP 200 与 5 个带坐标的地点，名称包含上海大学且均属于上海市；其中一个地点的底图返回 HTTP 200、有效 PNG。
- 联调使用专属临时普通账号，结束后清理该账号与其业务数据，保留原测试账号及会话。
- 部署沿用数据卷、数据库密码与 RSA 密钥；无需清空数据库或重新注册用户。

## 回退

在保留未提交修改后，可检出本次地点修复之前的提交 `65dc395` 并执行上述构建命令。该提交保留正文流式闪现修复，但能力代理仍有重复编码问题。配置文件与数据卷独立于 Git，回退代码不会删除它们；不要使用 `down -v`。
