# 高德地点能力与快速差旅接入回归记录

> 编号：BUG-2026-08-29-01 / BUG-2026-08-29-02 / BUG-2026-08-29-03 / BUG-2026-08-30-04 / BUG-2026-08-30-05 / BUG-2026-08-30-06
> 发现日期：2026-08-29 至 2026-08-30
> 状态：均已修复并加入回归测试

## 1. Capability 扩展导致 Goal 查询失去作用域

### 现象

`query-info` 从单一外部信息节点扩展为“天气/交通 + 地点信息”两个并行节点后，旧的
`TaskDecomposer._task_query()` 把所有多节点 intent 都当成工作流，直接返回完整用户问题。
当原问题同时包含“差旅标准”和“天气”时，`information_query` Goal 因携带“标准”等
禁用词被 scope validator 拒绝。

### 根因

旧代码用“执行节点数量大于 1”判断一个 intent 是否为工作流。地点能力使 capability
也可以合法拥有多个并行节点，该假设不再成立。

### 修复

- 完整差旅工作流继续保留原问题作为 Goal query；
- 多节点 capability 仍用其第一个受限模板生成 Goal query；
- 子节点继续由 Graph Builder 分别渲染各自查询，不改变核心 DAG 结构。

### 验证

`tests/test_task_orchestration_v2.py` 中混合制度与外部信息的作用域测试恢复通过，并增加
七节点差旅 DAG 的新预期。

## 2. 新请求参数破坏旧聊天调用兼容性

### 现象

聊天路由最初对所有请求都传递 `structured_trip_input=None`。旧适配器和部分测试桩只接受
原有关键字参数，因此普通聊天出现 `unexpected keyword argument`，被映射成 500。

### 根因

结构化参数本应只属于快速差旅请求，但第一次接线时把它无条件传给了 manager。

### 修复

- 只有 `input_source=quick_trip_form` 且存在已校验结构化数据时才传新参数；
- 普通对话保持原调用形状，不影响旧客户端和现有适配器；
- 普通 chat/stream、session、增强检索和附件路径均执行回归测试。

### 验证

`tests/test_webui_error_responses.py` 与 `tests/test_quick_trip.py` 合计 `28 passed`。

## 3. 地点关键词自动启用覆盖了显式关闭

### 现象与根因

“不要查附近酒店”既包含“附近酒店”正向关键词，也符合显式否定规则。第一次实现先自动
加入 `nearby_hotels`、再加入排除列表，而 Graph Builder 的既有契约规定 include 优先，
结果用户明确关闭后地点节点仍会执行。

### 修复与验证

默认关闭 capability 只有在“命中正向关键词且没有显式否定”时才能自动 include；显式
否定继续写入 exclude。`test_standalone_hotel_opt_out_does_not_reinclude_from_keyword_match`
直接覆盖了地点关键词与否定词同时出现的路径。

## 4. 市内路线结果被错误标记为天气

### 现象

`query-info` 的声明式回答类型原本固定为 `weather`。单独查询“上海虹桥站到静安寺怎么走”时，
即使执行节点返回了正确的高德公交路线，fallback composer 仍会把结果放进标题为“目的地天气”
的卡片。

### 根因

同一个 `information_query` capability 同时承载天气和公开交通，但旧的 fallback 渲染只根据
intent 的静态 `section_kind` 选择卡片，没有检查已规范化结果中的 `query_type` 和 `route`。

### 修复与验证

不增加用户 intent 或回答协议类型。仅当 `information_query` 明确返回 `query_type=市内交通`
或结构化 `route` 时，确定性渲染为“市内交通”通用区块；天气仍沿用现有天气卡。新增
`test_amap_transit_result_is_not_mislabeled_as_weather` 覆盖该路径。

## 5. 新路线工具名导致 Skill 清单校验失败

### 现象

为 `query-info` 增加高德路径规划后，初版清单把工具声明为新的 `route_planning`。SkillLoader
在应用启动和测试收集阶段拒绝该值，所有依赖 Skill 目录的模块均无法加载。

### 根因

本次实现只升级了 `travel_information` 的内部 provider，并没有增加新的平台级工具类型。清单
却越过现有 `ToolName` 枚举声明了新类型，破坏了启动期的严格配置校验。

### 修复与验证

继续使用既有 `travel_information` 工具标识，由 `query-info` 内部选择天气、路线或公共信息
provider，不扩展平台工具协议。`test_skill_catalog_derived.py`、`test_skill_platform.py` 和
`test_skill_registry.py` 覆盖清单解析与注册链路。

## 6. 海外天气降级沿用中国时区

### 现象与根因

Open-Meteo 原降级实现只覆盖内置中国城市，经纬度查询固定使用 `Asia/Shanghai`。升级为支持
海外城市动态地理编码后，如果继续沿用该参数，海外逐日预报的日期边界会按中国时区计算，
不符合目的地当地日期。

### 修复与验证

Open-Meteo 降级请求改为 `timezone=auto`，让供应商按解析后的目的地坐标选择当地时区。
`test_open_meteo_fallback_geocodes_overseas_city_in_local_timezone` 覆盖海外地理编码、当地时区
参数和天气结果生成。

## 7. 非产品故障

本地首次收集 WebUI 测试时，环境文件中的 PostgreSQL DSN 占位值造成连接池超时。切换到
测试用本地 RAG/记忆后端后测试正常。该问题属于测试运行配置，不是本次功能产生的产品
缺陷，因此不单独登记为产品 Bug。
