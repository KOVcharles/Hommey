# Hommey 小型压测

脚本：`scripts/load_test.py`。使用项目已有的 Python/httpx；不导入 Agent、不连接业务数据库、不自动启动服务。
针对当前 Spring Boot + Python 分离栈，默认经过 Nginx 的 `http://localhost:8088` 访问真实业务入口。

## 1. 准备

先按工程化指南启动服务，用一个已有的专用测试账号登录页面，确认会话列表可用、制度问答正常。
测试账号尽量配置好个人资料，保持测试问题和资料一致。脚本支持一个账号下多个独立会话；这不能证明多个用户之间的性能或隔离。

在 PowerShell 中进入仓库，用项目环境执行：

```powershell
Set-Location D:\Hommey
.\.venv\Scripts\python.exe scripts/load_test.py --help
```

每次传入 `--email` 后会提示隐藏输入密码，登录和身份查询不计入压测。不要把密码写进命令或报告。
如果已有 access token，可通过环境变量 `HOMMEY_LOAD_TOKEN` 提供，并省略 `--email`。
报告写入已被 Git 忽略的 `benchmark-results/`，只保存时延、状态和结果统计，不保存 token、问题正文、答案或个人资料。

另开一个终端观察应用资源：

```powershell
docker stats
```

报告旁记录机器 CPU/内存、Docker 资源限制、容器副本数、AI worker 数、实际全局并发配置、模型和 embedding 配置名称、测试账号的数据量及当天是否有其他负载。
同机压测要明确记录压测程序与服务共享资源，结果仅代表该环境。

## 2. 先测只读业务 API

第一次测试 10 QPS、60 秒：

```powershell
.\.venv\Scripts\python.exe scripts/load_test.py --email test@example.com api --target sessions --qps 10 --duration 60
```

把邮箱换成已有测试账号。每次自动预热 5 秒，先做一次响应格式检查，然后进入独立测量窗口。
每个到达槽位只请求一次目标接口，认证不在循环内，没有自动重试。

通过后逐档测试 `20 → 50 → 100` QPS，每档至少 60 秒；不要在一档失败后继续提高压力。

```powershell
.\.venv\Scripts\python.exe scripts/load_test.py --email test@example.com api --target sessions --qps 20 --duration 60
.\.venv\Scripts\python.exe scripts/load_test.py --email test@example.com api --target profile --qps 20 --duration 60
```

建议本轮先约定：普通 API 的成功响应 P95 ≤ 300 ms、失败比例 < 1%、无客户端丢弃。
这是自定的验收条件，不是已有性能结果。选取最高通过档位，再持续 5 分钟验证：

```powershell
.\.venv\Scripts\python.exe scripts/load_test.py --email test@example.com api --target sessions --qps 50 --duration 300
```

读报告时关注：

| 字段 | 含义 |
| --- | --- |
| `target_qps` | 想要施加的 QPS，不是测得的处理能力 |
| `dispatched_per_s` | 测量窗口内实际发起的请求速率 |
| `successful_completed_in_window_per_s` | 窗口内成功完成的请求数 / 窗口秒数 |
| `successful_per_s_including_drain` | 全部成功请求数 / 包含最后请求收尾的总时长 |
| `latency_successful_ms.p95` | 95% 的成功请求延迟不超过这个值 |
| `unsuccessful_rate` | 失败或响应不符合接口契约的比例 |
| `dropped_before_dispatch` | 压测端错过发起时机或达到最大在途数，因而没有发出的请求数 |
| `dispatch_lag_ms.p95` | 实际执行比计划发起时间晚多少 |

脚本按固定到达计划发起请求，而不是等上一条返回再发。
默认最多 100 个在途请求；超限或错过一个发起周期就记 dropped，不建立无限队列、不事后突发补发。
只要 dropped > 0，该档就不能声称已施加目标 QPS。先结合资源和延迟判断是压测端跟不上，还是服务变慢使在途请求满了。
脚本退出码 0 仅代表没有失败/丢弃，P95 门槛仍需查看报告。

会话列表中只有 0～1 条数据时，结果只能说明很小的数据集。要代表历史较多的账号，需要另选有代表性数据的测试账号。

## 3. 再测真实 AI 对话

先看 1 并发的正常完整回复，再测 2、4，最后酌情测 8：

```powershell
.\.venv\Scripts\python.exe scripts/load_test.py --email test@example.com chat --concurrency 1 --rounds 2
.\.venv\Scripts\python.exe scripts/load_test.py --email test@example.com chat --concurrency 2 --rounds 2
.\.venv\Scripts\python.exe scripts/load_test.py --email test@example.com chat --concurrency 4 --rounds 2
```

`--rounds 2` 是每个并发 worker 发 2 个问题，因此上面分别调用 2、4、8 个真实对话请求。
每个对话可能包含多次模型和工具调用。脚本会新建并保留这些会话/消息，请在测试账号运行，按模型用量控制次数。
每个请求用一个全新会话，避开同会话互斥，并保持相同的初始对话长度；会话创建不计入对话耗时。
这里采用固定并发的闭环测试：一个请求结束后，同一路才发送下一轮。它适合少量真实模型测试，不等同于固定到达速率的容量测试。

默认问题要求查询交通费报销材料与制度来源。也可以用 `--message '自定义问题'`；不同复杂度的问题应分开出报告。
首次单并发测试后在页面检查实际答案与来源，确保问题确实触发了预期业务链路。

成功条件：HTTP 200、收到非空正文或结构化内容、收到 `done`、`outcome=completed`，且没有错误事件或中断。
`waiting_input`、`partial`、`degraded` 等终态单独统计为未达到本次完整回答成功条件；它们不一定都是系统故障。
状态/进度事件不算首字。`first_text_successful_ms` 测第一个非空正文 chunk；只有卡片时该值为空，`first_content_successful_ms` 则记录第一份可展示内容。
正文可能被 `response_reset` 替换，因此首字仅反映首次展示正文，不代表最终答案已验证。
`latency_successful_ms` 是完整流结束时间，包含客户端可观察到的排队和初始化时间；没有单独测出内部排队耗时。
每条请求有总超时（默认 180 秒），既约束无响应，也约束长期不断流却不结束的情况。

AI 核心指标：`successful_turns_per_min`、完整耗时 P50/P95、首字/首内容耗时、`outcomes` 和失败代码。
2～16 个请求只能用于初步比较，尤其 P95/P99 在小样本下接近最大值，不能作为稳定百分位或持续容量证明。
正式写入面试材料前，在预算允许的情况下增加轮数，重复测试，并确认有效答案质量。

## 4. 记录结论

| 测试对象 | 环境和数据量 | 目标负载 | 实际成功吞吐 | P95 | 失败/未完成比例 | 瓶颈 |
| --- | --- | --- | --- | --- | --- | --- |
| 会话列表 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 |
| 个人资料 | 待填 | 待填 | 待填 | 待填 | 待填 | 待填 |
| 真实制度问答 | 待填 | 待填并发 | 待填轮/分钟 | 待填 | 待填 | 待填 |

只读接口报告只能支持对应接口和数据规模；短样本 AI 报告只支持该问题和该次并发测试。
若最高测试档仍满足条件，应表述为“在该负载下通过”，系统极限仍未知。
数据库连接等待、CPU 饱和、模型 429 等判断需要日志/监控佐证，不能只由客户端报告推断。

方法参考：[固定到达速率](https://grafana.com/docs/k6/latest/using-k6/scenarios/executors/constant-arrival-rate/)、[未发出的迭代](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/dropped-iterations/)、[HTTPX 流式响应](https://www.python-httpx.org/async/)。
