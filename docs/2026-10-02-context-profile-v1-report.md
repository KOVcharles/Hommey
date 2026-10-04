# 上下文与用户背景：第一版小幅修改报告

日期：2026-10-02。范围：现有偏好进入上下文、复用已确认工作地点、明确跨日车次日期。

本版已经完成代码修改、离线回归和三项真实模型针对性检查。保留现有存储和主/子 Agent 架构，先验证“已有用户信息能否低成本、稳定地被模型使用”。个人身份信息采集和个性化政策检索仍属于下一阶段。

## 1. 修改结果

| 项目 | 修改前 | 修改后 |
|---|---|---|
| 主 Agent 用户背景 | 初始快照没有偏好；部分业务代码单独读取 | 初始 `<runtime_context>` 增加小型 `user_profile`，首个模型调用即可看见 |
| 子 Agent 用户背景 | 委派背景主要依靠主 Agent 自行转述 | 宿主按角色自动传递所需的画像字段 |
| 本轮偏好更新 | 主 Agent 收到提交回执，但没有统一的新画像 | 成功提交后回执携带新画像，后续创建的子任务读取新值 |
| 已确认工作地点 | 普通酒店工具仍从关键词重新解析地点 | 增加显式复用已确认地点的查询方式，直接查附近酒店 |
| 跨日车次 | 模型自行理解日期、时刻和 `arrival_day_offset` | 程序生成 `departure_at`、`arrival_at`，明确日期及 `+08:00` 时区 |

本版没有数据库迁移、依赖安装、注册界面改动或用户资料批量改写。原有未提交工作保留。本报告和差异文件只描述本轮增量。

## 2. 用户背景如何进入上下文

新增 `agent_runtime/user_profile.py`，从现有 `long_term.get_preference()` 返回值构建白名单投影。

目前支持的七个字段：

- `home_location`：投影为 `defaults.origin`，作为常用出发地。
- `transportation_preference`、`seat_preference`、`meal_preference`、`budget_level`。
- `hotel_brands`、`airlines`。

示例为虚构测试资料，不是新建或修改的真实用户档案：

```json
{
  "user_profile": {
    "source": "saved_preferences",
    "defaults": {"origin": "上海"},
    "preferences": {
      "seat_preference": "商务座",
      "meal_preference": "清淡",
      "budget_level": "经济",
      "hotel_brands": ["如家"],
      "airlines": ["国航"]
    }
  }
}
```

### 字段控制

- 复用既有 `profile_catalog` 的类型、长度和敏感内容校验；单项字符串最多 120 字符。
- 每个品牌/航司列表最多常驻四项；更多项仍保留在原存储，投影通过 `truncated_fields` 标记省略，完整查询继续走记忆工具。
- 原校验允许最多十二项的列表；超过校验限制的历史异常值不会进入常驻投影。
- 未支持的字段、任意 `extra_preferences`、工号、学号等不会被直接复制进去。
- 无资料或读取失败时不构造虚假的用户背景；失败记录错误类型，回答流程仍可继续。模型规则明确缺字段表示未知。

这不是新增一份长期档案存储，也没有接入尚未贯通的 `user_profile_facts` 写入链路。现有偏好表仍是本版的数据来源，checkpoint 只保存执行所需的小型快照。

### 生命周期和更新

1. 每个新用户请求开始时读取一次已保存偏好。此前常住地预填也会读取这份数据，本次合并使用，不为常驻背景额外增加一次初始化读取。
2. 首次主模型调用前写入初始快照；每次调用渲染这份快照，不向聊天历史不断追加用户信息消息。
3. 同一请求恢复时沿用 checkpoint 中的画像；已完成请求的幂等重放直接使用原响应。
4. 下一条用户请求重新读取数据库，获得最新值。
5. 本轮通过 `apply_changes` 成功提交偏好后，更新运行状态并返回新 `user_profile`；初始快照保持不变，通过真实工具回执表达变化。
6. 后续新建子任务使用最新画像。涉及画像的委派去重标识也包含角色所见画像，减少更新后仍复用同一个旧委派的情况。

当前明确出发地优先于保存的默认出发地。例如默认上海、本次北京时，行程事实仍保留北京。席位和预算偏好只表达选择倾向，不构成交通等级或报销资格。

### 各角色收到什么

| 角色 | 本版注入内容 |
|---|---|
| 主 Agent | 常用出发地及支持的出行偏好 |
| `trip_context` | 常用出发地 |
| `travel_info` | 酒店品牌、航司、席位、交通方式偏好 |
| `memory` | 完整的精简投影；需要完整列表或记忆证据时仍可检索 |
| `trip_planner` | 常用出发地及支持的出行偏好 |
| `policy_rag` / `compliance` | 本版不注入偏好块；现有数据没有可靠的身份和职级字段，不能用偏好替代资格 |

## 3. 酒店查询如何复用已确认地点

上次记录里，已确认的广州大学校区没有作为可复用地点传给普通酒店查询，子 Agent 多次搜索关键词，最终又由候选方案流程重新查询。

本次改动：

- `trip_facts()` 在保存地点通过现有城市和名称一致性校验后，向模型提供 `work_location_confirmed: true`。
- `find_hotels` 增加可选参数 `use_trip_location`，默认 `false`，兼容原有关键词查询。
- 查询当前工作地点附近酒店时，模型设置该参数为 `true`；宿主把本轮真实行程交给业务适配器，由代码取出已验证地点。
- 地点不存在、城市不一致、名称变化后旧地点失效等情况返回明确错误，不默默搜索另一个地点。
- 用户查询其他会场时仍走原关键词路径。

模型只看到确认标记；新增输入不暴露地点供应商内部 ID，也不接受模型自行提交一个地点 ID 充当已验证地点。

测试确认该复用路径没有调用 `resolve_anchor`，直接调用附近酒店接口。真实模型检查也主动选择了 `use_trip_location=true`。本版未对真实地图网络延迟、酒店覆盖率或端到端总轮次做性能基准，不能据此宣称原六轮查询必然降成某个固定轮数。

## 4. 跨日车次处理

在规划输入中增加完整时间。例如：

```json
{
  "travel_date": "2026-10-02",
  "depart_time": "20:05",
  "arrive_time": "07:11",
  "arrival_day_offset": 1,
  "departure_at": "2026-10-02T20:05+08:00",
  "arrival_at": "2026-10-03T07:11+08:00"
}
```

完整日期由 Python 计算，处理同日、跨日和跨年；原候选及展示数据不被修改。日期格式无效、负偏移或到达早于出发时，标记 `date_status=invalid`，提示规划角色不要据此确定日程。

提示词明确：不能为赶上会议擅自把车次提前一天；日期冲突应解释冲突。

这里完成的是输入消歧和行为约束，没有新增对所有自然语言活动文本的程序级日期核验。因此不能声称已经消除全部日期幻觉。

## 5. 上下文开销

以下均为 `json.dumps(..., ensure_ascii=False)` 或文本长度统计，单位是字符，不是 token；未测真实账单或 token 缓存收益。

| 项目 | 实测字符数 |
|---|---:|
| 示例 `user_profile` JSON | 195 |
| 加入快照后运行时数据块增量（含键名等） | 213 |
| 主提示词使用规则增量 | 167 |
| 同一示例下主上下文总增量 | 380 |
| 行程收集子角色示例画像 | 61 |
| 出行查询子角色示例画像 | 118 |
| 最大合法长度测试样本的画像 | 1,841 |

另有子角色公共规则、酒店及日期使用规则的少量静态增量。本版是用少量常驻背景减少信息遗漏和重复查询，并非整个上下文压缩工程。没有删除历史对话、重写历史摘要或声称总体 token 消耗已下降。

度量文件：`tmp/debug/context-profile-v1-20261002/context-size.json`。

## 6. 验证结果

### 离线回归

最终结果：**214 passed，11 skipped，0 failed**。本次新增测试文件包含 **20 项测试**（含参数化案例）。

覆盖范围：

- 首次模型调用看见画像、下一轮刷新、幂等重放不重复读取。
- 明确出发地覆盖默认值；初始化不产生额外业务查询或资料写入。
- 空资料、存储故障、用户身份不匹配时不泄露其他用户偏好。
- 白名单、敏感和超长值过滤、列表省略标记及长度上限。
- 六角色真实调度代码中的画像裁剪和隔离。
- 偏好提交后回执及后续子任务看见新值，原始快照保持原值。
- 专业工具调度到业务服务时使用宿主行程中的地点。
- 地点缺失、城市不匹配、名称变化、跨用户查询时拒绝错误复用。
- 原有其他地点关键词查询仍可用。
- 同日、跨日、跨年及无效车次日期。
- 原有原生消息、对话路由、行程表单、执行控制、循环检测、工具发现、文本交付及状态简化测试。

执行命令：

```powershell
docker exec -e HOMMEY_RAG_SEARCH_SCOPES= hommey-app python -m pytest -q -ra --junitxml=/app/tmp/debug/context-profile-v1-20261002/offline-tests.xml tests/test_user_profile_context.py tests/test_native_context.py tests/test_supervisor_runtime.py tests/test_turn_context.py tests/test_dialogue_routing.py tests/test_trip_intake_experience.py tests/test_supervisor_context_bounds.py tests/test_supervisor_control.py tests/test_supervisor_loop_detection.py tests/test_tool_discovery.py tests/test_agent_text_replies.py tests/test_service_help.py tests/test_state_simplification.py
```

第一轮回归中，一项六角色测试受容器当前 `HOMMEY_RAG_SEARCH_SCOPES` 限制影响：虚构政策证据没有生产知识范围前缀，因而被现有校验判定不可复用。仅在独立测试进程清空该配置后通过；没有改常驻服务配置或放宽生产校验。

11 项跳过均为既有 opt-in 真实模型评测：历史上下文 2 项、文本回复 1 项、服务介绍 8 项。本版另做下述真实模型针对性检查，没有将这 11 项算作通过。

现有 pytest-asyncio 默认事件循环配置和 DashScope 弃用提示仍会出现，不是本次测试失败。

### 真实模型针对性检查

使用当前配置的 `deepseek-v4-flash`，通过独立 Python 进程运行，使用虚构偏好、行程和候选，未写入真实业务数据库。

| 检查 | 观察结果 | 范围 |
|---|---|---|
| 已保存偏好直接回答 | 一次主模型调用回答“如家、商务座”；无业务工具调用、无资料写入 | 使用真实 Supervisor、SDK 和模型，存储与业务接口使用测试替身 |
| 已确认地点查询 | 首次选择 `find_hotels`，参数包含 `use_trip_location=true` | 验证真实模型工具选择；没有调用地图网络服务 |
| 跨日规划 | 10 月 2 日出发、10 月 3 日到达，并指出无法满足 10 月 2 日会场工作的冲突 | 单个虚构候选；报告通过现有 schema 和引用校验 |

三项目标断言均通过。检查脚本为 `tmp/debug/context-profile-v1-20261002/verify_live.py`，结果为同目录的 `live-results.json`，并保存了 SDK 入参预览。

真实规划样本还暴露了一个既有不足：摘要说明缺少资料和时间冲突，但模型没有显式填写 `status=partial` 和 `missing_info`，现有 schema 默认值仍能通过校验；还有将“未提供早班候选”表述成“无早班车”的过度概括。因此本次只确认跨日日期目标改善，不将它视为完整规划质量验收。这部分建议下一版补充结构化完成条件校验。

## 7. 文件变更与增量边界

| 文件 | 本轮作用 |
|---|---|
| `agent_runtime/user_profile.py`（新增） | 白名单画像投影和角色裁剪 |
| `agent_runtime/engine.py` | 初始化画像、提交后更新、子任务传递及酒店调用的可信行程参数 |
| `agent_runtime/context_window.py` | 已确认工作地点的模型可见标记 |
| `agent_runtime/contracts.py` | 酒店查询可选模式参数 |
| `agent_runtime/services.py` | 在宿主已确认地点周边查询酒店 |
| `agent_runtime/full_trip.py` | 规划用车次的完整出发和到达时间 |
| `agent_runtime/prompts/main.md` | 主 Agent 使用用户背景的规则 |
| `agent_runtime/prompts/specialist-common.md` | 子 Agent 用户背景的含义和边界 |
| `agent_runtime/prompts/specialists/travel_info.md` | 已确认地点复用规则 |
| `agent_runtime/prompts/specialists/trip_planner.md` | 跨日日期和无效日期处理规则 |
| `tests/test_user_profile_context.py`（新增） | 20 项新增回归检查 |

相对本轮开始时的工作区：业务代码及提示词新增约 131 行、删除 15 行；新增测试文件 241 行。报告与调试材料另计。该数字不是相对 Git HEAD 的统计，因为本轮开始时工作区已有大量其他改动。

修改前副本保存在 `tmp/context-profile-v1-baseline-20261002/`。本轮单独差异保存在 `tmp/debug/context-profile-v1-20261002/this-turn.patch`，文件统计在 `change-summary.json`。若需撤回，应以这些差异逐项核对，不能用整个工作区的 reset/checkout 抹掉已有改动。

## 8. 尚未纳入本版的部分

1. **个人身份档案**：机构、人员类别、职称、岗位等级、学生类别、学号/工号尚未新增；当前 `user_profile` 名称表示未来可扩展的模型视图，不代表完整个人档案已经上线。
2. **首次资料引导**：注册后资料表、字段编辑页和身份核验仍待实现。
3. **个性化 RAG**：尚未将机构、人员和经费条件接入结构化检索过滤及适用性计算；本版不会因为席位偏好而改变报销资格。
4. **统一档案存储**：现有 `user_profile_facts` 与 `user_travel_preferences` 的主数据归一尚未进行。
5. **跨轮报告失效**：本版将画像纳入新委派的去重标识，但没有完成所有历史报告针对画像版本变化的自动失效机制；也不会重写已经开始执行的子任务。
6. **历史精简和规划投影压缩**：旧无关问答、重复证据、酒店照片等字段的进一步优化尚未实施。
7. **规划输出强校验**：自然语言日期核验、缺项与 `partial` 状态一致性仍需增强。

下一版建议优先增加“人员类别＋专业职称/岗位等级＋常用出发城市”的轻量资料表和明确来源，再将这些字段传给制度检索。学号/工号用于身份关联，通常不需要常驻模型上下文。

## 9. 运行状态

代码已经写入当前工作区，并由容器内新启动的独立进程验证。**常驻 `hommey-app` 服务没有重启，本次不能认定浏览器正在使用的 worker 已加载新代码。** 项目当前以双 worker 且不启用热重载的方式运行，Python 和提示词改动需要在合适的时间重启服务才能在页面生效。

本次没有创建 Git 提交或发布操作。后台服务重启也会加载工作区内其他既有 Python 改动，实际启用时应一并考虑。
