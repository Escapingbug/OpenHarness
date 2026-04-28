"""Tool for writing persistent memory entries."""

from __future__ import annotations

from openharness.memory import add_memory_entry
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field


class MemoryWriteToolInput(BaseModel):
    """Arguments for memory writes."""

    title: str = Field(description="Short title for the memory entry (used as filename)")
    content: str = Field(description="Markdown content to store")


class MemoryWriteTool(BaseTool):
    """Write a persistent memory entry that survives across sessions."""

    name = "memory_write"
    description = "Store a durable memory entry. Use to remember facts, decisions, or context across sessions."
    input_model = MemoryWriteToolInput

    async def execute(self, arguments: MemoryWriteToolInput, context: ToolExecutionContext) -> ToolResult:
        path = add_memory_entry(context.cwd, arguments.title, arguments.content)
        return ToolResult(output=f"Saved memory entry: {path.name}")
