"""
MCP Tool Agent
通过 MCPManager 调用外部 MCP Server 的工具。
在 Orchestrator 的调度下，作为 mcp-tool Skill 运行。
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from core.execution_budget import ExecutionLimitExceeded, consume_external_call
from typing import Optional, Union, List, Dict, Any
import json
import logging

logger = logging.getLogger(__name__)


class MCPToolAgent(AgentBase):
    """
    MCP 工具调用 Agent

    接收来自 Orchestrator 的调度任务，解析工具调用参数，
    通过 MCPManager 路由到目标 MCP Server 执行工具。
    """

    def __init__(
        self,
        name: str = "MCPToolAgent",
        model=None,
        mcp_manager=None,
        **kwargs,
    ):
        super().__init__()
        self.name = name
        self.model = model
        self.mcp_manager = mcp_manager

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        """
        执行 MCP 工具调用

        输入格式（来自 Orchestrator 调度任务）：
        {
            "server_name": "filesystem",
            "tool_name": "write_file",
            "arguments": {"path": "/tmp/test.txt", "content": "hello"},
            "execution_timeout": 30
        }

        返回格式：
        {
            "agent_name": "MCPToolAgent",
            "status": "success" | "error",
            "server_name": "filesystem",
            "tool_name": "write_file",
            "result": ...,
            "error": "..."
        }
        """
        if self.mcp_manager is None:
            return Msg(
                name=self.name,
                content=json.dumps({
                    "agent_name": self.name,
                    "status": "error",
                    "error": "MCPManager not initialized"
                }, ensure_ascii=False),
                role="assistant",
            )

        # 解析输入
        task_data = self._parse_input(x)

        server_name = task_data.get("server_name", "")
        tool_name = task_data.get("tool_name", "")
        arguments = task_data.get("arguments", {})
        execution_timeout = task_data.get("execution_timeout", None)

        if not server_name or not tool_name:
            return Msg(
                name=self.name,
                content=json.dumps({
                    "agent_name": self.name,
                    "status": "error",
                    "error": f"Missing server_name or tool_name: server={server_name}, tool={tool_name}"
                }, ensure_ascii=False),
                role="assistant",
            )

        # 调用 MCP 工具
        try:
            consume_external_call("mcp")
            result = await self.mcp_manager.call_tool(
                server_name=server_name,
                tool_name=tool_name,
                arguments=arguments,
                execution_timeout=execution_timeout,
            )

            # 提取文本内容（MCPToolFunction 返回的可能有 content 列表）
            result_text = self._extract_text(result)

            return Msg(
                name=self.name,
                content=json.dumps({
                    "agent_name": self.name,
                    "status": "success",
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "result": result_text,
                }, ensure_ascii=False),
                role="assistant",
            )

        except ExecutionLimitExceeded:
            raise
        except Exception as e:
            logger.error(f"MCP tool call failed: {server_name}.{tool_name} - {e}")
            return Msg(
                name=self.name,
                content=json.dumps({
                    "agent_name": self.name,
                    "status": "error",
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "error": str(e),
                }, ensure_ascii=False),
                role="assistant",
            )

    def _parse_input(self, x) -> Dict[str, Any]:
        """解析输入消息为任务字典，支持直接调用和 task_params 两种模式"""
        if x is None:
            return {}

        if isinstance(x, list):
            raw = x[-1].content if x else "{}"
        else:
            raw = x.content if hasattr(x, 'content') else str(x)

        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return {}
        elif isinstance(raw, dict):
            data = raw
        else:
            return {}

        # 模式 1: 直接包含 server_name/tool_name（直接调用模式）
        if "server_name" in data and "tool_name" in data:
            return data

        # 模式 2: 从 task_params 提取（Orchestrator 调度模式）
        task_params = data.get("task_params", {})
        if task_params:
            return task_params

        # 模式 3: 从 context 的 rewritten_query 推断（LLM 辅助模式，后续扩展）
        return {}

    def _extract_text(self, result) -> str:
        """从 MCP 工具结果中提取文本"""
        if result is None:
            return ""

        # 如果是 ToolResponse，取其 content
        if hasattr(result, 'content'):
            items = result.content
            if isinstance(items, list):
                texts = []
                for item in items:
                    if hasattr(item, 'text'):
                        texts.append(item.text)
                    elif isinstance(item, dict) and 'text' in item:
                        texts.append(item['text'])
                    elif isinstance(item, str):
                        texts.append(item)
                return "\n".join(texts) if texts else str(result)

        # 如果是 CallToolResult
        if hasattr(result, 'content'):
            content = result.content
            if isinstance(content, list):
                texts = []
                for item in content:
                    if hasattr(item, 'text'):
                        texts.append(item.text)
                    elif isinstance(item, dict) and 'text' in item:
                        texts.append(item['text'])
                    elif isinstance(item, str):
                        texts.append(item)
                return "\n".join(texts) if texts else str(result)

        return str(result)
