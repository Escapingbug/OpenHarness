"""Tests for McpRemoveTool — runtime MCP server removal with cleanup."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from openharness.config.settings import Settings, load_settings, save_settings
from openharness.mcp.client import McpClientManager
from openharness.mcp.types import McpStdioServerConfig
from openharness.tools.base import ToolExecutionContext, ToolRegistry
from openharness.tools.mcp_remove_tool import McpRemoveTool, McpRemoveToolInput


@pytest.mark.asyncio
async def test_remove_cleans_registry_and_persists(tmp_path: Path, monkeypatch):
    """Removing a server deletes it from settings and unregisters its tools."""
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    server_script = Path(__file__).resolve().parents[1] / "fixtures" / "fake_mcp_server.py"
    save_settings(
        Settings(
            mcp_servers={
                "fixture": McpStdioServerConfig(command=sys.executable, args=[str(server_script)]),
            }
        )
    )

    manager = McpClientManager(load_settings().mcp_servers)
    await manager.connect_all()
    registry = ToolRegistry()
    # Simulate registration of the discovered tool
    from openharness.tools.mcp_tool import McpToolAdapter
    for tool_info in manager.list_tools():
        registry.register(McpToolAdapter(manager, tool_info))

    context = ToolExecutionContext(
        cwd=tmp_path,
        metadata={"mcp_manager": manager, "tool_registry": registry},
    )

    try:
        assert registry.get("mcp__fixture__hello") is not None

        result = await McpRemoveTool().execute(
            McpRemoveToolInput(server_name="fixture"),
            context,
        )

        assert result.is_error is False
        assert "removed" in result.output

        # Cleaned from settings
        assert "fixture" not in load_settings().mcp_servers

        # Cleaned from registry
        assert registry.get("mcp__fixture__hello") is None

        # Cleaned from manager
        assert "fixture" not in manager._server_configs
        assert "fixture" not in manager._statuses
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_remove_unknown_returns_error():
    """Removing a non-existent server returns an error."""
    manager = McpClientManager({})
    registry = ToolRegistry()
    context = ToolExecutionContext(
        cwd=Path("."),
        metadata={"mcp_manager": manager, "tool_registry": registry},
    )

    result = await McpRemoveTool().execute(
        McpRemoveToolInput(server_name="ghost"),
        context,
    )

    assert result.is_error is True
    assert "not found" in result.output.lower()
