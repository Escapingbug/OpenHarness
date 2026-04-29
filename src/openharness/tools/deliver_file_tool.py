"""Deliver a file to the user via the chat channel."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class DeliverFileToolInput(BaseModel):
    """Arguments for the deliver_file tool."""

    path: str = Field(description="Path of the file to deliver to the user")


class DeliverFileTool(BaseTool):
    """Send a file to the user through the chat channel.

    Use this when the user should receive a file (image, document, etc.)
    as an attachment in their chat. Not needed for code files the user
    can access in the workspace — only for deliverables the user asked
    for or would expect to receive.
    """

    name = "deliver_file"
    description = "Deliver a file to the user as a chat attachment. Use for images, documents, or other files the user should receive."
    input_model = DeliverFileToolInput

    async def execute(
        self,
        arguments: DeliverFileToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        path = _resolve_path(context.cwd, arguments.path)

        if not path.exists():
            return ToolResult(output=f"File not found: {path}", is_error=True)

        if path.is_dir():
            return ToolResult(output=f"Path is a directory, not a file: {path}", is_error=True)

        return ToolResult(
            output=f"File queued for delivery: {path}",
            metadata={"produced_files": [str(path)]},
        )


def _resolve_path(base: Path, candidate: str) -> Path:
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()
