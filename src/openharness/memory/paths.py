"""Paths for persistent project memory."""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from openharness.config.paths import get_data_dir

logger = logging.getLogger(__name__)

# Pattern matching the old "{name}-{hex12}" directory format.
_LEGACY_DIR_RE = re.compile(r"^(.+)-([0-9a-f]{12})$")


def get_project_memory_dir(cwd: str | Path) -> Path:
    """Return the persistent memory directory for a project.

    Uses the project directory name as the key so that memory is portable
    across machines (as long as the project name is unique).

    On first access after the naming change, any legacy directory using the
    old ``{name}-{sha1_hash}`` format is automatically migrated to the new
    ``{name}`` format.
    """
    path = Path(cwd).resolve()
    new_dir = get_data_dir() / "memory" / path.name

    # --- Legacy migration ---
    # Find and migrate any old-style directory that matches this project.
    # Also migrate if new_dir exists but is empty (e.g. created by an earlier
    # partial migration or mkdir call) while legacy content remains.
    if not new_dir.exists() or not any(new_dir.iterdir()):
        _migrate_legacy_dir(path.name, new_dir)

    new_dir.mkdir(parents=True, exist_ok=True)
    return new_dir


def _migrate_legacy_dir(project_name: str, new_dir: Path) -> None:
    """Migrate a legacy ``{name}-{hash}`` directory to the new ``{name}`` format.

    If a legacy directory exists for this project, rename it.  If multiple
    legacy directories match (unlikely), merge their contents into the new
    directory.
    """
    memory_base = get_data_dir() / "memory"
    if not memory_base.is_dir():
        return

    legacy_dirs: list[Path] = []
    for child in memory_base.iterdir():
        if not child.is_dir():
            continue
        m = _LEGACY_DIR_RE.match(child.name)
        if m and m.group(1) == project_name:
            legacy_dirs.append(child)

    if not legacy_dirs:
        return

    if len(legacy_dirs) == 1:
        # Simple case: just rename.
        try:
            legacy_dirs[0].rename(new_dir)
            logger.info(
                "Migrated memory directory: %s -> %s",
                legacy_dirs[0].name,
                new_dir.name,
            )
        except OSError:
            logger.debug(
                "Failed to rename legacy memory dir %s, falling back to copy",
                legacy_dirs[0],
                exc_info=True,
            )
            _merge_into(legacy_dirs[0], new_dir)
    else:
        # Multiple legacy dirs: merge all into the new directory.
        for legacy in legacy_dirs:
            _merge_into(legacy, new_dir)


def _merge_into(src: Path, dst: Path) -> None:
    """Copy files from *src* into *dst*, then remove *src*.

    Existing files in *dst* are not overwritten.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            if target.exists():
                _merge_into(item, target)
            else:
                shutil.move(str(item), str(target))
        elif item.is_file():
            if not target.exists():
                shutil.move(str(item), str(target))
    # Remove the source directory if it's now empty.
    try:
        src.rmdir()  # only removes empty dirs
    except OSError:
        pass


def get_memory_entrypoint(cwd: str | Path) -> Path:
    """Return the project memory entrypoint file."""
    return get_project_memory_dir(cwd) / "MEMORY.md"
