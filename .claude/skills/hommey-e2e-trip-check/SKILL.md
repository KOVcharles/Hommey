---
name: hommey-e2e-trip-check
description: 对本地 Hommey 栈跑一次完整企业差旅 E2E（发起→补全行程→合规检查三连轮），从 supervisor_runs 提取 answer_document 评估完善度（车次/天气/制度/规划是否齐全、有无 TURN_TIMEOUT）。当用户要求测试差旅链路、评估完善度、或复现"没有 12306 车票内容"类问题时使用。
---

# Hommey 完整差旅 E2E 检查

用真实 HTTP 流式接口驱动完整差旅链路，并输出可判读的完善度摘要。
只创建 `e2e-*@hommey.local` 测试用户，不触碰其他用户数据。

## 1. 栈就绪（约 1 分钟）

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml ps
curl -s http://127.0.0.1:8000/readyz
```

- hommey-app 必须 healthy；Docker Desktop 未跑则先启动，compose 的 `restart: unless-stopped` 会自动拉起。
- 若未起来：`docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d`

## 2. 轮次预算前置条件

确认 `.env` 里有 `HOMMEY_SUPERVISOR_TURN_TIMEOUT_SEC=120`（默认 60 时，制度+记忆+车次天气+规划的完整轮次会在 travel_info 之前就 TURN_TIMEOUT，表现就是"差旅没有车票"）。

改过 .env 后必须重建容器才会注入：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d --force-recreate hommey
docker exec hommey-app env | grep SUPERVISOR_TURN   # 验证
```

## 3. 跑驱动（约 2-3 分钟）

```bash
docker exec -i hommey-app python - < .claude/skills/hommey-e2e-trip-check/e2e_driver.py
```

输出：`user_id`/`session_id` 两行 + T1/T2/T3 三轮 JSON（`elapsed_s` / `outcome` / `stop_reason` / `steps` / `tasks` / `assess`）。

三连轮定义：
- T1「我要去南京出差」→ 预期 intake 卡片，`outcome=waiting_input`，0.1s 内
- T2「从北京出发，2026-09-13出发，出差2天，出差目的：参加客户会议」→ 完整流水线，75-90s
- T3「帮我检查一下这个行程是否符合制度要求」→ 合规检查，40-70s

## 4. 完善度判读标准（主要看 T2 的 assess）

| 信号 | 通过标准 |
|---|---|
| `stop_reason` | 不是 `TURN_TIMEOUT`；`outcome` 不是 `degraded` |
| `steps` | 含 行程保存(succeeded) + 查询差旅标准 + 整理天气与交通信息 + 核对差旅记录与偏好 + 生成出差行程 |
| `assess.has_trains` | true，且 `train_samples` 有 G/C/D 开头车次（说明 12306 真实数据进来了）；最佳形态是出现 kind=train 的独立卡片节（`has_train_section`） |
| `assess.has_itinerary` | true（行程规划节成功，逐日日程引用具体车次） |
| `assess.policy_amounts` | > 0（制度结论带具体金额，且卡片带来源） |
| `assess.timeout_marks` | 0（正文无"达到时间上限/预算已用完"字样） |

T1 `waiting_input` 是预期。T3 波动较大（三次实测分别为 completed / waiting_input(带追问) / degraded(NO_PROGRESS)）：只要 `compliance` 步骤 succeeded 且正文含合规结论就算过；`NO_PROGRESS` 是主 Agent 重复委派被引擎保护性拦截，已交付内容仍在。

## 5. 已知波动与判读

- 主 Agent 路由不确定：T3 偶尔误派 `trip_context`（步骤 failed 但自愈，不致命）；T2 的 travel_info 在 succeeded/partial 之间波动，常见形态是第一次委派返回天气、主 Agent 二次委派补车次（所以 T2 可能 75-95s，两种都算通过）。
- 制度检索结论波动：南京城市等级在不同检索路径下可能"二类城市"或"待确认"（HyDE/标准检索路径差异），属于 RAG 不稳定信号，值得在报告中标注。
- 车次只有余票无票价：`prices` 为空是已知未实现（queryTicketPrice 后置阶段）。
- 新用户无偏好/职级 → memory 如实报未知，属正常。
- 若单轮 >110s 仍有 TURN_TIMEOUT，优先看 policy/memory 子 Agent 回读轮数（profiles.py 中"最多四轮"）与 LLM 端点延迟。

## 6. 清理测试用户（可选）

```sql
DELETE FROM users WHERE email LIKE 'e2e-%@hommey.local';
```
