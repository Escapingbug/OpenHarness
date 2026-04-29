"""Tests for McpAddTool — runtime MCP server addition with persistence."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from openharness.config.settings import Settings, load_settings, save_settings
from openharness.mcp.client import McpClientManager
from openharness.mcp.types import McpStdioServerConfig
from openharness.tools.base import ToolExecutionContext, ToolRegistry
from openharness.tools.mcp_add_tool import McpAddTool, McpAddToolInput
from openharness.tools.mcp_tool import McpToolAdapter


@pytest.mark.asyncio
async def test_add_stdio_persists_and_registers_tools(tmp_path: Path, monkeypatch):
    """Adding a stdio server persists to settings and registers discovered tools."""
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(Settings())

    server_script = Path(__file__).resolve().parents[1] / "fixtures" / "fake_mcp_server.py"
    manager = McpClientManager({})
    registry = ToolRegistry()
    context = ToolExecutionContext(
        cwd=tmp_path,
        metadata={"mcp_manager": manager, "tool_registry": registry},
    )

    result = await McpAddTool().execute(
        McpAddToolInput(
            server_name="fixture",
            type="stdio",
            command=sys.executable,
            args=[str(server_script)],
        ),
        context,
    )

    try:
        assert result.is_error is False
        assert "connected" in result.output

        # Persisted to settings
        saved = load_settings().mcp_servers.get("fixture")
        assert saved is not None
        assert isinstance(saved, McpStdioServerConfig)
        assert saved.command == sys.executable

        # Registered in tool registry
        hello_tool = registry.get("mcp__fixture__hello")
        assert hello_tool is not None
        assert isinstance(hello_tool, McpToolAdapter)

        # Tool is executable
        tool_result = await hello_tool.execute(
            hello_tool.input_model.model_validate({"name": "world"}),
            ToolExecutionContext(cwd=tmp_path),
        )
        assert tool_result.output == "fixture-hello:world"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_add_duplicate_returns_error():
    """Adding a server that already exists returns an error."""
    manager = McpClientManager({"existing": McpStdioServerConfig(command="python", args=[])})
    registry = ToolRegistry()
    context = ToolExecutionContext(
        cwd=Path("."),
        metadata={"mcp_manager": manager, "tool_registry": registry},
    )

    result = await McpAddTool().execute(
        McpAddToolInput(server_name="existing", type="stdio", command="python"),
        context,
    )

    assert result.is_error is True
    assert "already exists" in result.output


@pytest.mark.asyncio
async def test_add_without_manager_returns_error():
    """Missing mcp_manager in metadata returns a clear error."""
    context = ToolExecutionContext(cwd=Path("."), metadata={})

    result = await McpAddTool().execute(
        McpAddToolInput(server_name="x", type="stdio", command="python"),
        context,
    )

    assert result.is_error is True
    assert "mcp_manager" in result.output.lower()


def test_add_is_not_read_only():
    """McpAddTool is a mutating tool (requires permission check)."""
    tool = McpAddTool()
    assert tool.is_read_only(McpAddToolInput(server_name="x", type="stdio", command="python")) is False
