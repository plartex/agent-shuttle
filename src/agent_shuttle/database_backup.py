"""Consistent backups for database schema upgrades."""
import os
import sqlite3
import stat
import uuid
from contextlib import closing
from pathlib import Path


def backup_database(path: Path) -> Path:
    """Save a consistent SQLite snapshot before modifying an existing database."""
    backup = path.with_name(f"{path.name}.schema-backup-{uuid.uuid4().hex}.sqlite3")
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as destination:
        source.backup(destination)
    if os.name != "nt":
        os.chmod(backup, stat.S_IMODE(path.stat().st_mode) & 0o600)
    return backup
