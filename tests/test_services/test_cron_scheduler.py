"""Tests for the cron scheduler daemon."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from openharness.services.cron_scheduler import (
    _jobs_due,
    append_history,
    execute_job,
    load_history,
    run_scheduler_loop,
)


@pytest.fixture(autouse=True)
def _tmp_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect data and log directories to temp."""
    data_dir = tmp_path / "data"
    logs_dir = tmp_path / "logs"
    data_dir.mkdir()
    logs_dir.mkdir()
    monkeypatch.setattr("openharness.services.cron_scheduler.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("openharness.services.cron_scheduler.get_logs_dir", lambda: logs_dir)
    # Also redirect the cron registry used by the scheduler
    monkeypatch.setattr(
        "openharness.services.cron.get_cron_registry_path",
        lambda: data_dir / "cron_jobs.json",
    )


class TestHistory:
    def test_empty_history(self) -> None:
        assert load_history() == []

    def test_append_and_load(self) -> None:
        append_history({"name": "j1", "status": "success"})
        append_history({"name": "j2", "status": "failed"})
        entries = load_history()
        assert len(entries) == 2
        assert entries[0]["name"] == "j1"

    def test_filter_by_name(self) -> None:
        append_history({"name": "j1", "status": "success"})
        append_history({"name": "j2", "status": "success"})
        entries = load_history(job_name="j1")
        assert len(entries) == 1
        assert entries[0]["name"] == "j1"

    def test_limit(self) -> None:
        for i in range(10):
            append_history({"name": f"j{i}", "status": "success"})
        entries = load_history(limit=3)
        assert len(entries) == 3
        # Should be the last 3
        assert entries[0]["name"] == "j7"


class TestJobsDue:
    def test_due_job(self) -> None:
        now = datetime.now(timezone.utc)
        past = (now - timedelta(minutes=5)).isoformat()
        jobs = [
            {"name": "j1", "schedule": "* * * * *", "enabled": True, "next_run": past},
        ]
        due = _jobs_due(jobs, now)
        assert len(due) == 1

    def test_future_job_not_due(self) -> None:
        now = datetime.now(timezone.utc)
        future = (now + timedelta(hours=1)).isoformat()
        jobs = [
            {"name": "j1", "schedule": "* * * * *", "enabled": True, "next_run": future},
        ]
        due = _jobs_due(jobs, now)
        assert len(due) == 0

    def test_disabled_job_not_due(self) -> None:
        now = datetime.now(timezone.utc)
        past = (now - timedelta(minutes=5)).isoformat()
        jobs = [
            {"name": "j1", "schedule": "* * * * *", "enabled": False, "next_run": past},
        ]
        due = _jobs_due(jobs, now)
        assert len(due) == 0

    def test_invalid_schedule_skipped(self) -> None:
        now = datetime.now(timezone.utc)
        past = (now - timedelta(minutes=5)).isoformat()
        jobs = [
            {"name": "j1", "schedule": "not valid", "enabled": True, "next_run": past},
        ]
        due = _jobs_due(jobs, now)
        assert len(due) == 0

    def test_missing_next_run_skipped(self) -> None:
        now = datetime.now(timezone.utc)
        jobs = [
            {"name": "j1", "schedule": "* * * * *", "enabled": True},
        ]
        due = _jobs_due(jobs, now)
        assert len(due) == 0


class TestExecuteJob:
    @pytest.mark.asyncio
    async def test_successful_job(self) -> None:
        job = {"name": "echo-test", "command": "echo hello", "cwd": "/tmp"}
        entry = await execute_job(job)
        assert entry["status"] == "success"
        assert entry["returncode"] == 0
        assert "hello" in entry["stdout"]

    @pytest.mark.asyncio
    async def test_failing_job(self) -> None:
        job = {"name": "fail-test", "command": "exit 1", "cwd": "/tmp"}
        entry = await execute_job(job)
        assert entry["status"] == "failed"
        assert entry["returncode"] == 1

    @pytest.mark.asyncio
    async def test_timeout_job(self) -> None:
        with patch("openharness.services.cron_scheduler.asyncio.wait_for") as mock_wait:
            import asyncio

            mock_wait.side_effect = asyncio.TimeoutError()

            # Need to mock create_subprocess_exec to return a mock process
            mock_process = AsyncMock()
            mock_process.communicate = Mock(return_value=object())
            mock_process.kill = Mock()
            mock_process.wait = AsyncMock()
            with patch(
                "openharness.utils.shell.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ):
                job = {"name": "slow-test", "command": "sleep 999", "cwd": "/tmp"}
                entry = await execute_job(job)
                assert entry["status"] == "timeout"


class TestExecuteAgentJob:
    """Tests for agent-type cron job execution (requires runtime_pool + bus)."""

    @pytest.mark.asyncio
    async def test_agent_job_with_runtime_pool(self) -> None:
        """Agent job should call runtime_pool.stream_message and publish outbound."""
        from openharness.channels.bus.events import OutboundMessage, InboundMessage
        from ohmo.gateway.runtime import GatewayStreamUpdate

        # Mock runtime_pool — stream_message must be an async generator
        mock_pool = AsyncMock()
        stream_calls: list[tuple] = []

        async def _fake_stream(message, session_key):
            stream_calls.append((message, session_key))
            yield GatewayStreamUpdate(
                kind="progress",
                text="Thinking...",
                metadata={"_progress": True, "_session_key": session_key},
            )
            yield GatewayStreamUpdate(
                kind="final",
                text="⏰ 提醒：review PR",
                metadata={"_session_key": session_key},
            )

        mock_pool.stream_message = _fake_stream

        # Mock bus
        mock_bus = AsyncMock()

        job = {
            "name": "remind-review",
            "command": "",
            "type": "agent",
            "prompt": "提醒用户 review PR",
            "session_key": "telegram:12345:user1",
            "channel": "telegram",
            "chat_id": "12345",
            "cwd": "/tmp",
        }
        entry = await execute_job(job, runtime_pool=mock_pool, bus=mock_bus)

        assert entry["status"] == "success"
        assert entry["name"] == "remind-review"
        # stream_message should have been called with a synthetic InboundMessage
        assert len(stream_calls) == 1
        inbound_msg, session_key = stream_calls[0]
        assert inbound_msg.content == "提醒用户 review PR"
        assert inbound_msg.channel == "telegram"
        assert inbound_msg.chat_id == "12345"
        # Outbound should have been published
        mock_bus.publish_outbound.assert_called()

    @pytest.mark.asyncio
    async def test_agent_job_without_runtime_pool(self) -> None:
        """Agent job without runtime_pool should record error, not crash."""
        job = {
            "name": "agent-no-pool",
            "command": "",
            "type": "agent",
            "prompt": "hello",
            "session_key": "t:1:u",
            "channel": "t",
            "chat_id": "1",
            "cwd": "/tmp",
        }
        entry = await execute_job(job)
        assert entry["status"] == "error"
        assert "runtime_pool" in entry["stderr"].lower() or "no runtime" in entry["stderr"].lower()

    @pytest.mark.asyncio
    async def test_shell_job_unchanged_with_runtime_pool(self) -> None:
        """Shell job should still use subprocess even when runtime_pool is provided."""
        mock_pool = AsyncMock()
        mock_bus = AsyncMock()

        job = {"name": "shell-with-pool", "command": "echo still-shell", "cwd": "/tmp"}
        entry = await execute_job(job, runtime_pool=mock_pool, bus=mock_bus)

        assert entry["status"] == "success"
        assert "still-shell" in entry["stdout"]
        # runtime_pool should NOT have been called
        mock_pool.stream_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_job_stream_error(self) -> None:
        """Agent job should handle stream_message exceptions gracefully."""
        mock_pool = AsyncMock()

        async def _failing_stream(message, session_key):
            raise RuntimeError("LLM API error")
            yield  # make this an async generator

        mock_pool.stream_message = _failing_stream
        mock_bus = AsyncMock()

        job = {
            "name": "agent-error",
            "command": "",
            "type": "agent",
            "prompt": "hello",
            "session_key": "t:1:u",
            "channel": "t",
            "chat_id": "1",
            "cwd": "/tmp",
        }
        entry = await execute_job(job, runtime_pool=mock_pool, bus=mock_bus)
        assert entry["status"] == "error"


class TestSchedulerLoop:
    @pytest.mark.asyncio
    async def test_once_mode_with_no_jobs(self) -> None:
        """Scheduler loop in once-mode should complete without error when no jobs exist."""
        await run_scheduler_loop(once=True, skip_signal_handlers=True)

    @pytest.mark.asyncio
    async def test_once_mode_fires_due_job(self) -> None:
        """Scheduler loop should fire a job that is due."""
        from openharness.services.cron import upsert_cron_job

        upsert_cron_job({"name": "test-once", "schedule": "* * * * *", "command": "echo fired"})

        # Force next_run to the past so it's immediately due
        from openharness.services.cron import load_cron_jobs, save_cron_jobs

        jobs = load_cron_jobs()
        now = datetime.now(timezone.utc)
        jobs[0]["next_run"] = (now - timedelta(minutes=1)).isoformat()
        save_cron_jobs(jobs)

        await run_scheduler_loop(once=True, skip_signal_handlers=True)

        entries = load_history(job_name="test-once")
        assert len(entries) == 1
        assert entries[0]["status"] == "success"

    @pytest.mark.asyncio
    async def test_once_mode_skip_signal_handlers(self) -> None:
        """run_scheduler_loop with skip_signal_handlers=True should work on Windows."""
        # This should not raise NotImplementedError on Windows
        await run_scheduler_loop(once=True, skip_signal_handlers=True)
