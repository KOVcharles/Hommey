---
name: mcp-tool
description: Route an exact MCP server, tool, and argument request only after the caller has independently authorized that operation in an internal business-travel workflow. Do not use for general filesystem access, open-ended external actions, or inferred tool calls.
---

# MCP Tool (外部工具调用)

通过 MCP 协议调用外部工具。当前支持的工具由 `MCPManager` 动态发现。

## When to Use

- 上游已经提供明确的 `server_name`、`tool_name` 和参数
- 对应操作已经由调用方完成独立授权

## Agent

- **MCPToolAgent** — 接收工具调用请求，路由到 MCPManager
- 入参为 **model 对象** + **mcp_manager 实例**
- **异步**：`reply()` 为 `async`，需 `await`

## 调用模式

1. IntentionAgent 识别到 MCP 工具意图 → 在 `agent_schedule` 中添加 `mcp_tool` 任务
2. Orchestrator 调度到 `MCPToolAgent`
3. MCPToolAgent 解析参数 → 调用 `mcp_manager.call_tool(server, tool, arguments)`
4. 返回结果给 Orchestrator 聚合

## 安全边界

- 当前执行器不实现逐工具授权策略；启用该 Skill 前必须由部署方限制可连接的 MCP Server 和工具。
- 不根据自然语言自行扩大参数、选择额外工具或连续执行未授权操作。
- 不执行付款、预订、审批、报销提交或凭据读取。
