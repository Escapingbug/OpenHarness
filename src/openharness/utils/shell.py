"""Shared shell and subprocess helpers."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath

from openharness.config import Settings, load_settings
from openharness.platforms import PlatformName, get_platform


def resolve_shell_command(
    command: str,
    *,
    platform_name: PlatformName | None = None,
    prefer_pty: bool = False,
    settings: Settings | None = None,
) -> list[str]:
    """Return argv for the best available shell on the current platform."""
    resolved_platform = platform_name or get_platform()
    resolved_settings = settings or Settings()
    if resolved_platform == "windows":
        for candidate in resolved_settings.shell.windows_preference:
            argv = _argv_for_windows_shell(candidate, command)
            if argv is not None:
                return argv
        return [shutil.which("cmd.exe") or "cmd.exe", "/d", "/s", "/c", command]

    for candidate in resolved_settings.shell.posix_preference:
        shell = _resolve_shell_path(candidate)
        if shell:
            argv = _argv_for_posix_shell(shell, command)
            if _is_powershell(shell):
                argv = [shell, "-NoLogo", "-NoProfile", "-Command", command]
            if _is_cmd(shell):
                continue
            if prefer_pty:
                wrapped = _wrap_command_with_script(argv, platform_name=resolved_platform)
                if wrapped is not None:
                    return wrapped
            return argv

    shell = shutil.which("sh") or os.environ.get("SHELL") or "/bin/sh"
    argv = [shell, "-lc", command]
    if prefer_pty:
        wrapped = _wrap_command_with_script(argv, platform_name=resolved_platform)
        if wrapped is not None:
            return wrapped
    return argv


def wrap_command_for_sandbox(
    command: list[str],
    *,
    settings: Settings | None = None,
) -> tuple[list[str], Path | None]:
    from openharness.sandbox.adapter import (
        wrap_command_for_sandbox as _wrap_command_for_sandbox,
    )

    return _wrap_command_for_sandbox(command, settings=settings)


def _argv_for_windows_shell(candidate: str, command: str) -> list[str] | None:
    shell = _resolve_shell_path(candidate)
    if shell is None:
        return None
    if _is_bash_like(shell):
        return [shell, "-lc", command]
    if _is_powershell(shell):
        return [shell, "-NoLogo", "-NoProfile", "-Command", command]
    if _is_cmd(shell):
        return [shell, "/d", "/s", "/c", command]
    return None


def _argv_for_posix_shell(shell: str, command: str) -> list[str]:
    return [shell, "-lc", command]


def _resolve_shell_path(candidate: str) -> str | None:
    shell = candidate.strip()
    if not shell:
        return None
    if shell.lower() == "cmd":
        shell = "cmd.exe"
    resolved = shutil.which(shell)
    if resolved:
        return resolved
    path = Path(shell).expanduser()
    if path.exists():
        return str(path)
    return None


def _shell_name(shell: str) -> str:
    path_name = Path(shell).name
    windows_name = PureWindowsPath(shell).name
    name = windows_name if "\\" in shell else path_name
    return name.lower().removesuffix(".exe")


def _is_bash_like(shell: str) -> bool:
    return _shell_name(shell) in {"bash", "sh"}


def _is_powershell(shell: str) -> bool:
    return _shell_name(shell) in {"pwsh", "powershell"}


def _is_cmd(shell: str) -> bool:
    return _shell_name(shell) == "cmd"


async def create_shell_subprocess(
    command: str,
    *,
    cwd: str | Path,
    settings: Settings | None = None,
    prefer_pty: bool = False,
    stdin: int | None = asyncio.subprocess.DEVNULL,
    stdout: int | None = None,
    stderr: int | None = None,
    env: Mapping[str, str] | None = None,
) -> asyncio.subprocess.Process:
    """Spawn a shell command with platform-aware shell selection and sandboxing."""
    resolved_settings = settings or load_settings()

    # Docker backend: route through docker exec
    if resolved_settings.sandbox.enabled and resolved_settings.sandbox.backend == "docker":
        from openharness.sandbox.session import get_docker_sandbox

        session = get_docker_sandbox()
        if session is not None and session.is_running:
            argv = resolve_shell_command(command, settings=resolved_settings)
            return await session.exec_command(
                argv,
                cwd=cwd,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                env=dict(env) if env is not None else None,
            )
        if resolved_settings.sandbox.fail_if_unavailable:
            from openharness.sandbox import SandboxUnavailableError

            raise SandboxUnavailableError("Docker sandbox session is not running")

    # Existing srt path
    argv = resolve_shell_command(command, prefer_pty=prefer_pty, settings=resolved_settings)
    argv, cleanup_path = wrap_command_for_sandbox(argv, settings=resolved_settings)

    try:
        process = await async_subprocess_exec(
            *argv,
            cwd=str(Path(cwd).resolve()),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=dict(env) if env is not None else None,
        )
    except Exception:
        if cleanup_path is not None:
            cleanup_path.unlink(missing_ok=True)
        raise

    if cleanup_path is not None:
        asyncio.create_task(_cleanup_after_exit(process, cleanup_path))
    return process


def _wrap_command_with_script(
    argv: list[str],
    *,
    platform_name: PlatformName | None = None,
) -> list[str] | None:
    resolved_platform = platform_name or get_platform()
    if resolved_platform == "macos":
        return None
    script = shutil.which("script")
    if script is None:
        return None
    if len(argv) >= 3 and argv[1] == "-lc":
        return [script, "-qefc", argv[2], "/dev/null"]
    return None


async def _cleanup_after_exit(process: asyncio.subprocess.Process, cleanup_path: Path) -> None:
    try:
        await process.wait()
    finally:
        cleanup_path.unlink(missing_ok=True)


def _win_creationflags(overrides: int = 0) -> dict:
    """Return creationflags for subprocess calls on Windows.

    If *overrides* is provided (e.g. ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``),
    ``CREATE_NO_WINDOW`` is OR'd into it.  On non-Windows platforms an empty dict
    is returned.
    """
    if sys.platform == "win32":
        return {"creationflags": overrides | subprocess.CREATE_NO_WINDOW}
    return {}


def subprocess_run(*args, **kwargs):
    """Wrapper around ``subprocess.run`` that suppresses the console window
    on Windows (``CREATE_NO_WINDOW``).  Drop-in replacement: change
    ``subprocess.run(...)`` to ``subprocess_run(...)``.
    """
    existing_flags = kwargs.pop("creationflags", 0)
    merged = {**kwargs, **_win_creationflags(existing_flags)}
    return subprocess.run(*args, **merged)


def subprocess_popen(*args, **kwargs):
    """Wrapper around ``subprocess.Popen`` that suppresses the console window
    on Windows (``CREATE_NO_WINDOW``).  Drop-in replacement: change
    ``subprocess.Popen(...)`` to ``subprocess_popen(...)``.
    """
    existing_flags = kwargs.pop("creationflags", 0)
    merged = {**kwargs, **_win_creationflags(existing_flags)}
    return subprocess.Popen(*args, **merged)


def subprocess_check_output(*args, **kwargs):
    """Wrapper around ``subprocess.check_output`` that suppresses the console
    window on Windows (``CREATE_NO_WINDOW``).  Drop-in replacement: change
    ``subprocess.check_output(...)`` to ``subprocess_check_output(...)``.
    """
    existing_flags = kwargs.pop("creationflags", 0)
    merged = {**kwargs, **_win_creationflags(existing_flags)}
    return subprocess.check_output(*args, **merged)


async def async_subprocess_exec(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
    """Wrapper around ``asyncio.create_subprocess_exec`` that suppresses the
    console window on Windows (``CREATE_NO_WINDOW``).  Drop-in replacement:
    change ``asyncio.create_subprocess_exec(*cmd, ...)`` to
    ``await async_subprocess_exec(*cmd, ...)``.
    """
    existing_flags = kwargs.pop("creationflags", 0)
    merged = {**kwargs, **_win_creationflags(existing_flags)}
    return await asyncio.create_subprocess_exec(*argv, **merged)
