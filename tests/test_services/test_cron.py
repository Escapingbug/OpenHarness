"""Tests for cron registry helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from openharness.services.cron import (
    delete_cron_job,
    get_cron_job,
    load_cron_jobs,
    mark_job_run,
    next_run_time,
    set_job_enabled,
    upsert_cron_job,
    validate_cron_expression,
)


@pytest.fixture(autouse=True)
def _tmp_cron_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect cron registry to a temp directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(
        "openharness.services.cron.get_cron_registry_path",
        lambda: data_dir / "cron_jobs.json",
    )


class TestValidation:
    def test_valid_expressions(self) -> None:
        assert validate_cron_expression("* * * * *")
        assert validate_cron_expression("*/5 * * * *")
        assert validate_cron_expression("0 9 * * 1-5")
        assert validate_cron_expression("0 0 1 1 *")

    def test_invalid_expressions(self) -> None:
        assert not validate_cron_expression("")
        assert not validate_cron_expression("every 5 minutes")
        assert not validate_cron_expression("60 * * * *")
        assert not validate_cron_expression("* * * *")  # only 4 fields

    def test_next_run_time(self) -> None:
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        nxt = next_run_time("0 * * * *", base)
        assert nxt == datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)


class TestCRUD:
    def test_empty_load(self) -> None:
        assert load_cron_jobs() == []

    def test_upsert_and_load(self) -> None:
        upsert_cron_job({"name": "test-job", "schedule": "*/5 * * * *", "command": "echo hi"})
        jobs = load_cron_jobs()
        assert len(jobs) == 1
        assert jobs[0]["name"] == "test-job"
        assert jobs[0]["enabled"] is True
        assert "next_run" in jobs[0]
        assert "created_at" in jobs[0]

    def test_upsert_replaces(self) -> None:
        upsert_cron_job({"name": "j1", "schedule": "* * * * *", "command": "echo 1"})
        upsert_cron_job({"name": "j1", "schedule": "0 * * * *", "command": "echo 2"})
        jobs = load_cron_jobs()
        assert len(jobs) == 1
        assert jobs[0]["command"] == "echo 2"

    def test_delete(self) -> None:
        upsert_cron_job({"name": "j1", "schedule": "* * * * *", "command": "echo 1"})
        assert delete_cron_job("j1") is True
        assert load_cron_jobs() == []

    def test_delete_missing(self) -> None:
        assert delete_cron_job("nope") is False

    def test_get_job(self) -> None:
        upsert_cron_job({"name": "j1", "schedule": "* * * * *", "command": "echo 1"})
        job = get_cron_job("j1")
        assert job is not None
        assert job["name"] == "j1"

    def test_get_missing(self) -> None:
        assert get_cron_job("nope") is None

    def test_sorted_output(self) -> None:
        upsert_cron_job({"name": "z-job", "schedule": "* * * * *", "command": "z"})
        upsert_cron_job({"name": "a-job", "schedule": "* * * * *", "command": "a"})
        jobs = load_cron_jobs()
        assert [j["name"] for j in jobs] == ["a-job", "z-job"]


class TestToggle:
    def test_enable_disable(self) -> None:
        upsert_cron_job({"name": "j1", "schedule": "* * * * *", "command": "echo 1"})
        assert set_job_enabled("j1", False) is True
        job = get_cron_job("j1")
        assert job is not None
        assert job["enabled"] is False

        assert set_job_enabled("j1", True) is True
        job = get_cron_job("j1")
        assert job is not None
        assert job["enabled"] is True

    def test_toggle_missing(self) -> None:
        assert set_job_enabled("nope", True) is False


class TestMarkRun:
    def test_mark_success(self) -> None:
        upsert_cron_job({"name": "j1", "schedule": "*/5 * * * *", "command": "echo ok"})
        mark_job_run("j1", success=True)
        job = get_cron_job("j1")
        assert job is not None
        assert job["last_status"] == "success"
        assert "last_run" in job

    def test_mark_failure(self) -> None:
        upsert_cron_job({"name": "j1", "schedule": "*/5 * * * *", "command": "false"})
        mark_job_run("j1", success=False)
        job = get_cron_job("j1")
        assert job is not None
        assert job["last_status"] == "failed"

    def test_mark_missing_is_noop(self) -> None:
        # Should not raise
        mark_job_run("nope", success=True)


class TestAgentJobSchema:
    """Tests for agent-type cron jobs (type='agent' with context, session_key, etc.)."""

    def test_upsert_agent_job(self) -> None:
        """Agent job with type='agent' should persist all extra fields."""
        upsert_cron_job({
            "name": "remind-review",
            "schedule": "0 9 * * 1-5",
            "command": "",
            "type": "agent",
            "context": "用户在 10:00 请求你在 10:10 提醒他 review PR",
            "session_key": "telegram:12345:user1",
            "channel": "telegram",
            "chat_id": "12345",
            "once": True,
        })
        job = get_cron_job("remind-review")
        assert job is not None
        assert job["type"] == "agent"
        assert job["context"] == "用户在 10:00 请求你在 10:10 提醒他 review PR"
        assert job["session_key"] == "telegram:12345:user1"
        assert job["channel"] == "telegram"
        assert job["chat_id"] == "12345"
        assert job["once"] is True

    def test_agent_job_enabled_and_next_run(self) -> None:
        """Agent job should still get enabled=True and next_run computed."""
        upsert_cron_job({
            "name": "agent-job",
            "schedule": "0 * * * *",
            "command": "",
            "type": "agent",
            "context": "check status",
            "session_key": "test:1:user",
            "channel": "telegram",
            "chat_id": "1",
        })
        job = get_cron_job("agent-job")
        assert job is not None
        assert job["enabled"] is True
        assert "next_run" in job

    def test_shell_job_has_no_extra_fields(self) -> None:
        """Regular shell job should not have agent-specific fields."""
        upsert_cron_job({"name": "shell-job", "schedule": "* * * * *", "command": "echo hi"})
        job = get_cron_job("shell-job")
        assert job is not None
        assert job.get("type") is None
        assert job.get("context") is None

    def test_agent_job_replaces_shell_job_same_name(self) -> None:
        """Replacing a shell job with an agent job of same name should work."""
        upsert_cron_job({"name": "j1", "schedule": "* * * * *", "command": "echo 1"})
        upsert_cron_job({
            "name": "j1",
            "schedule": "* * * * *",
            "command": "",
            "type": "agent",
            "context": "hello",
            "session_key": "t:1:u",
            "channel": "t",
            "chat_id": "1",
        })
        jobs = load_cron_jobs()
        assert len(jobs) == 1
        assert jobs[0]["type"] == "agent"

    def test_agent_job_mark_run(self) -> None:
        """mark_job_run should work on agent jobs."""
        upsert_cron_job({
            "name": "agent-mark",
            "schedule": "*/5 * * * *",
            "command": "",
            "type": "agent",
            "context": "test",
            "session_key": "t:1:u",
            "channel": "t",
            "chat_id": "1",
        })
        mark_job_run("agent-mark", success=True)
        job = get_cron_job("agent-mark")
        assert job is not None
        assert job["last_status"] == "success"


class TestCorruptData:
    def test_corrupt_json(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        bad_file = tmp_path / "data" / "cron_jobs.json"
        bad_file.parent.mkdir(parents=True, exist_ok=True)
        bad_file.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(
            "openharness.services.cron.get_cron_registry_path",
            lambda: bad_file,
        )
        assert load_cron_jobs() == []

    def test_non_list_json(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        bad_file = tmp_path / "data" / "cron_jobs.json"
        bad_file.parent.mkdir(parents=True, exist_ok=True)
        bad_file.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        monkeypatch.setattr(
            "openharness.services.cron.get_cron_registry_path",
            lambda: bad_file,
        )
        assert load_cron_jobs() == []
