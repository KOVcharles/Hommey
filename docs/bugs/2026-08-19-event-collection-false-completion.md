# 行程字段收集被误报为完整行程 Bug 记录

> 编号：BUG-2026-08-19-02<br>
> 发现日期：2026-08-19<br>
> 状态：代码已修复，待部署验证<br>
> 解决方案状态：已实施（2026-08-22）

## 1. 用户现象

用户输入“出发地北京、2天、客户拜访、8月20日”。第一次请求返回“处理失败”；
再次发送相同内容后，页面显示“本次出差 / 已整理好行程安排”，但只有一个空的
“行程安排”区块，没有提示仍缺少目的地。

## 2. 实际执行证据

- 失败请求 `c439f179-2143-4c86-b4f5-ce3f58242bc3` 在加载 `event_collection`
  时发生 `ImportError: cannot import name 'BEIJING_TIMEZONE'`。
- 同一输入重试后，日志显示只有 `event_collection` 执行成功，没有执行行程规划、
  政策 RAG、天气或车次查询。
- 输入只包含出发地、日期、时长和目的，目的地仍然缺失，`planning_ready` 应为
  `false`。

## 3. 根因

1. 运行容器中的 `core` 代码与挂载进容器的 Skill 文件版本不一致；多 worker 中一个
   worker 首次加载新 Skill 时导入失败，另一个已缓存旧模块的 worker 仍可继续执行，
   导致相同请求表现不一致。
2. 独立 `event_collection` 没有声明 `planning_ready=false` 的暂停规则；Agent 的
   “字段提取成功”被管线当成“业务完成”。
3. `event_collection` 被映射为 `trip` 展示区，Fallback Composer 在没有 itinerary 时
   自动生成“行程安排”标题和“已整理好行程安排”摘要，形成假成功空卡。

## 4. 修复内容

- 将 `event_collection` 收敛为工作流内部能力，禁止意图模型创建独立的信息收集任务。
- 为所有调用 `event_collection` 的业务入口增加 `planning_ready` 暂停契约。
- `planning_ready=false` 时由 Composer 兜底生成 `partial` 的行程补充卡，不再展示完整行程语义。
- 增加旧版内部任务状态兼容清理，保留已确认的 `active_trip` 字段。

## 5. 验收要求

- 上述输入稳定显示已确认的 4 项信息，并只询问缺失的目的地。
- 相同请求连续命中任意 worker 时结果一致，不出现间歇性导入失败。
- `planning_ready=false` 时不存在“已整理好行程安排”或空行程区块。
- 补齐目的地后，只有原始目标是 `itinerary_planning` 时才恢复完整规划工作流。

## 6. 剩余风险

修复暂停规则只能阻止假成功，不能代替运行版本一致性治理；只要生产环境仍允许
Skill 与核心代码独立热更新，多 worker 仍可能出现不同的导入状态和执行结果。

## 7. 本地验证记录

- 意图、状态、记忆 Hook、展示和编排定向测试：120 passed。
- 无外部依赖完整测试：595 passed，27 skipped。
- 临时 Redis 环境下协调与并发测试：10 passed。
- `git diff --check` 通过。
