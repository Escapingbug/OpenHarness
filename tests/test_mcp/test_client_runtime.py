"""Tests for McpClientManager runtime add/remove operations."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from openharness.mcp.client import McpClientManager
from openharness.mcp.types import McpStdioServerConfig, McpHttpServerConfig


@pytest.mark.asyncio
async def test_add_and_connect_stdio_success():
    """add_and_connect with a real stdio server discovers tools and updates status."""
    server_script = Path(__file__).resolve().parents[1] / "fixtures" / "fake_mcp_server.py"
    manager = McpClientManager({})
    config = McpStdioServerConfig(command=sys.executable, args=[str(server_script)])

    status = await manager.add_and_connect("fixture", config)

    try:
        assert status.state == "connected"
        assert status.transport == "stdio"
        assert len(status.tools) == 1
        assert status.tools[0].name == "hello"
        assert len(status.resources) == 1
        assert status.resources[0].uri == "fixture://readme"

        statuses = manager.list_statuses()
        assert len(statuses) == 1
        assert "fixture" in manager._server_configs
        assert "fixture" in manager._sessions
        assert "fixture" in manager._stacks
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_add_and_connect_fails_gracefully():
    """add_and_connect with a bad command sets failed status instead of raising."""
    manager = McpClientManager({})
    config = McpStdioServerConfig(command="this_command_does_not_exist_12345", args=[])

    status = await manager.add_and_connect("bad", config)

    assert status.state == "failed"
    assert "bad" in manager._statuses
    assert manager._sessions.get("bad") is None
    assert manager._stacks.get("bad") is None
    # cleanup should be safe
    await manager.close()


@pytest.mark.asyncio
async def test_remove_server_disconnects_and_cleans_up():
    """remove_server closes the session and removes all internal state."""
    manager = McpClientManager({})
    mock_stack = MagicMock()
    mock_stack.aclose = AsyncMock()
    mock_session = AsyncMock()

    manager._server_configs["gone"] = McpStdioServerConfig(command="python", args=[])
    manager._statuses["gone"] = MagicMock()
    manager._stacks["gone"] = mock_stack
    manager._sessions["gone"] = mock_session

    await manager.remove_server("gone")

    assert "gone" not in manager._server_configs
    assert "gone" not in manager._statuses
    assert "gone" not in manager._stacks
    assert "gone" not in manager._sessions
    mock_stack.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_duplicate_overwrites():
    """Adding a server with an existing name closes the old connection and replaces it."""
    manager = McpClientManager({})
    old_stack = MagicMock()
    old_stack.aclose = AsyncMock()
    old_session = AsyncMock()

    manager._server_configs["dup"] = McpStdioServerConfig(command="old", args=[])
    manager._statuses["dup"] = MagicMock()
    manager._stacks["dup"] = old_stack
    manager._sessions["dup"] = old_session

    server_script = Path(__file__).resolve().parents[1] / "fixtures" / "fake_mcp_server.py"
    config = McpStdioServerConfig(command=sys.executable, args=[str(server_script)])

    status = await manager.add_and_connect("dup", config)

    try:
        assert status.state == "connected"
        old_stack.aclose.assert_awaited_once()
        assert manager._sessions["dup"] is not old_session
    finally:
        await manager.close()
