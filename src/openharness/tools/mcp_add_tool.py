"""Tool for adding MCP servers at runtime."""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.config.settings import load_settings, save_settings
from openharness.mcp.client import McpClientManager
from openharness.mcp.types import McpHttpServerConfig, McpStdioServerConfig
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.tools.mcp_tool import McpToolAdapter


class McpAddToolInput(BaseModel):
    """Arguments for adding an MCP server."""

    server_name: str = Field(description="Unique name for the MCP server")
    type: str = Field(description="Transport type: stdio or http")
    command: str | None = Field(default=None, description="Command for stdio transport")
    args: list[str] = Field(default_factory=list, description="Arguments for stdio transport")
    url: str | None = Field(default=None, description="URL for http transport")
    env: dict[str, str] | None = Field(default=None, description="Environment variables for stdio transport")
    headers: dict[str, str] | None = Field(default=None, description="HTTP headers for http transport")


class McpAddTool(BaseTool):
    """Add a new MCP server at runtime, connect to it, and register its tools."""

    name = "mcp_add"
    description = "Add a new MCP server at runtime, connect to it, and register its tools."
    input_model = McpAddToolInput

    def is_read_only(self, arguments: McpAddToolInput) -> bool:
        del arguments
        return False

    async def execute(self, arguments: McpAddToolInput, context: ToolExecutionContext) -> ToolResult:
        mcp_manager = context.metadata.get("mcp_manager")
        if mcp_manager is None:
            return ToolResult(output="mcp_manager not available in tool metadata", is_error=True)

        tool_registry = context.metadata.get("tool_registry")
        if tool_registry is None:
            return ToolResult(output="tool_registry not available in tool metadata", is_error=True)

        if isinstance(mcp_manager, McpClientManager):
            existing = mcp_manager.get_server_config(arguments.server_name)
        else:
            existing = getattr(mcp_manager, "get_server_config", lambda n: None)(arguments.server_name)
        if existing is not None:
            return ToolResult(
                output=f"MCP server '{arguments.server_name}' already exists. Remove it first.",
                is_error=True,
            )

        if arguments.type == "stdio":
            if not arguments.command:
                return ToolResult(output="stdio transport requires 'command'", is_error=True)
            config = McpStdioServerConfig(
                command=arguments.command,
                args=arguments.args,
                env=arguments.env,
            )
        elif arguments.type == "http":
            if not arguments.url:
                return ToolResult(output="http transport requires 'url'", is_error=True)
            config = McpHttpServerConfig(
                url=arguments.url,
                headers=arguments.headers or {},
            )
        else:
            return ToolResult(output=f"Unsupported MCP transport type: {arguments.type}", is_error=True)

        try:
            status = await mcp_manager.add_and_connect(arguments.server_name, config)
        except Exception as exc:
            return ToolResult(output=f"Failed to connect MCP server: {exc}", is_error=True)

        if status.state == "failed":
            return ToolResult(
                output=f"MCP server '{arguments.server_name}' connection failed: {status.detail}",
                is_error=True,
            )

        # Persist to settings
        settings = load_settings()
        settings.mcp_servers[arguments.server_name] = config
        save_settings(settings)

        # Register newly discovered tools in the registry
        for tool_info in mcp_manager.list_tools():
            if tool_info.server_name != arguments.server_name:
                continue
            adapter_name = f"mcp__{_sanitize(tool_info.server_name)}__{_sanitize(tool_info.name)}"
            if tool_registry.get(adapter_name) is None:
                tool_registry.register(McpToolAdapter(mcp_manager, tool_info))

        tool_names = [t.name for t in status.tools]
        return ToolResult(
            output=f"MCP server '{arguments.server_name}' connected. Tools: {tool_names}"
        )


def _sanitize(value: str) -> str:
    import re

    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    if not sanitized:
        return "tool"
    if not sanitized[0].isalpha():
        return f"mcp_{sanitized}"
    return sanitized
