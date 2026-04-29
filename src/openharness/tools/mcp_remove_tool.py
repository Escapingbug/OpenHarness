"""Tool for removing MCP servers at runtime."""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.config.settings import load_settings, save_settings
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class McpRemoveToolInput(BaseModel):
    """Arguments for removing an MCP server."""

    server_name: str = Field(description="Name of the MCP server to remove")


class McpRemoveTool(BaseTool):
    """Remove an MCP server at runtime, disconnect it, and unregister its tools."""

    name = "mcp_remove"
    description = "Remove an MCP server at runtime, disconnect it, and unregister its tools."
    input_model = McpRemoveToolInput

    def is_read_only(self, arguments: McpRemoveToolInput) -> bool:
        del arguments
        return False

    async def execute(self, arguments: McpRemoveToolInput, context: ToolExecutionContext) -> ToolResult:
        mcp_manager = context.metadata.get("mcp_manager")
        if mcp_manager is None:
            return ToolResult(output="mcp_manager not available in tool metadata", is_error=True)

        tool_registry = context.metadata.get("tool_registry")
        if tool_registry is None:
            return ToolResult(output="tool_registry not available in tool metadata", is_error=True)

        if isinstance(mcp_manager, object):
            getter = getattr(mcp_manager, "get_server_config", None)
            existing = getter(arguments.server_name) if callable(getter) else None
        else:
            existing = None

        if existing is None:
            return ToolResult(
                output=f"MCP server '{arguments.server_name}' not found.",
                is_error=True,
            )

        # Unregister tools before disconnecting
        prefix = f"mcp__{arguments.server_name}__"
        for tool_name in list(getattr(tool_registry, "_tools", {}).keys()):
            if tool_name.startswith(prefix):
                del tool_registry._tools[tool_name]

        # Also unregister the generic list/read tools if this was the last MCP server
        # (We keep them since they work across all servers)

        try:
            remover = getattr(mcp_manager, "remove_server", None)
            if remover is None:
                return ToolResult(output="mcp_manager does not support remove_server", is_error=True)
            await remover(arguments.server_name)
        except Exception as exc:
            return ToolResult(output=f"Failed to remove MCP server: {exc}", is_error=True)

        # Remove from persisted settings
        settings = load_settings()
        settings.mcp_servers.pop(arguments.server_name, None)
        save_settings(settings)

        return ToolResult(output=f"MCP server '{arguments.server_name}' removed.")
