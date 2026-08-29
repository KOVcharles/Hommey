# 高德地点能力与快速差旅接入回归记录

> 编号：BUG-2026-08-29-01 / BUG-2026-08-29-02 / BUG-2026-08-29-03
> 发现日期：2026-08-29
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

## 4. 非产品故障

本地首次收集 WebUI 测试时，环境文件中的 PostgreSQL DSN 占位值造成连接池超时。切换到
测试用本地 RAG/记忆后端后测试正常。该问题属于测试运行配置，不是本次功能产生的产品
缺陷，因此不单独登记为产品 Bug。
