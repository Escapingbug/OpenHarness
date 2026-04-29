"""Tool for creating local cron-style jobs."""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.services.cron import set_job_enabled, upsert_cron_job, validate_cron_expression
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class CronCreateToolInput(BaseModel):
    """Arguments for cron job creation."""

    name: str = Field(description="Unique cron job name")
    schedule: str = Field(
        description=(
            "Cron schedule expression (e.g. '*/5 * * * *' for every 5 minutes, "
            "'0 9 * * 1-5' for weekdays at 9am)"
        ),
    )
    command: str | None = Field(
        default=None,
        description="Shell command to run when triggered. Required for shell-type jobs, ignored for agent-type.",
    )
    type: str | None = Field(
        default=None,
        description=(
            "Job type: 'agent' to have the AI send a message in the current chat session "
            "when triggered, or omit for a shell command job."
        ),
    )
    context: str | None = Field(
        default=None,
        description=(
            "For agent-type jobs: the context to inject when the job fires. "
            "Describe what the AI needs to know to act — e.g. '用户在 10:00 请求你在 10:10 提醒他 review PR'. "
            "The AI will see this as a message in its session and respond naturally."
        ),
    )
    once: bool = Field(
        default=False,
        description="If true, disable the job after it fires once (one-shot reminder).",
    )
    cwd: str | None = Field(default=None, description="Optional working directory override")
    enabled: bool = Field(default=True, description="Whether the job is active")


class CronCreateTool(BaseTool):
    """Create or replace a local cron job."""

    name = "cron_create"
    description = (
        "Create or replace a local cron job with a standard cron expression. "
        "Use type='agent' to schedule an AI-powered reminder in the current chat. "
        "Use 'oh cron start' to run the scheduler daemon."
    )
    input_model = CronCreateToolInput

    async def execute(
        self,
        arguments: CronCreateToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        if not validate_cron_expression(arguments.schedule):
            return ToolResult(
                output=(
                    f"Invalid cron expression: {arguments.schedule!r}\n"
                    "Use standard 5-field format: minute hour day month weekday\n"
                    "Examples: '*/5 * * * *' (every 5 min), '0 9 * * 1-5' (weekdays 9am)"
                ),
                is_error=True,
            )

        if arguments.type == "agent":
            if not arguments.context:
                return ToolResult(
                    output="Agent-type jobs require a 'context' field describing what the AI should know when the job fires.",
                    is_error=True,
                )
            # Auto-fill session info from gateway context
            session_info = context.metadata.get("_gateway_session") or {}
            job_dict: dict = {
                "name": arguments.name,
                "schedule": arguments.schedule,
                "command": "",
                "type": "agent",
                "context": arguments.context,
                "session_key": session_info.get("session_key", ""),
                "channel": session_info.get("channel", ""),
                "chat_id": session_info.get("chat_id", ""),
                "message_thread_id": session_info.get("message_thread_id", ""),
                "cwd": arguments.cwd or str(context.cwd),
                "enabled": arguments.enabled,
            }
            if arguments.once:
                job_dict["once"] = True
        else:
            # Shell-type job
            if not arguments.command:
                return ToolResult(
                    output="Shell-type jobs require a 'command' field.",
                    is_error=True,
                )
            job_dict = {
                "name": arguments.name,
                "schedule": arguments.schedule,
                "command": arguments.command,
                "cwd": arguments.cwd or str(context.cwd),
                "enabled": arguments.enabled,
            }
            if arguments.once:
                job_dict["once"] = True

        upsert_cron_job(job_dict)
        job_type = "agent" if arguments.type == "agent" else "shell"
        once_tag = " (one-shot)" if arguments.once else ""
        status = "enabled" if arguments.enabled else "disabled"
        return ToolResult(
            output=f"Created {job_type} cron job '{arguments.name}' [{arguments.schedule}] ({status}{once_tag})"
        )
