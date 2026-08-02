"""Private SQLite state persistence helpers for ephemeral runners."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def restore_state(state_directory: Path, database_path: Path) -> bool:
    """Restore a private database file when a previous successful run exists."""
    stored_database = state_directory / database_path.name
    if not stored_database.is_file():
        return False
    database_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(stored_database, database_path)
    return True


def persist_state(database_path: Path, state_directory: Path) -> Path:
    """Atomically copy private state back to the persistent state checkout."""
    state_directory.mkdir(parents=True, exist_ok=True)
    target = state_directory / database_path.name
    handle, temporary_name = tempfile.mkstemp(prefix=f".{target.name}-", dir=state_directory)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(database_path, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
