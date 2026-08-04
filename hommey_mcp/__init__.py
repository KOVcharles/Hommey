"""
MCP (Model Context Protocol) 集成模块
通过 MCPManager 连接外部 MCP Server，供 Web 后端消费工具。
"""
from .mcp_config import MCPConfig, MCPServerConfig
from .mcp_manager import MCPManager

__all__ = [
    "MCPConfig",
    "MCPServerConfig",
    "MCPManager",
]
