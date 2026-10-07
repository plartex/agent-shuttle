"""One-time migration of Agent Shuttle's pre-0.7 stored metadata."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
from contextlib import closing
from pathlib import Path

from google.protobuf.message import Message


OLD_PREFIX = "agent_bridge."
NEW_PREFIX = "agent_shuttle."


def backup_database(path: Path) -> Path:
    """Save a consistent SQLite snapshot before modifying an existing database."""
    backup = path.with_name(f"{path.name}.pre-0.7-{uuid.uuid4().hex}.sqlite3")
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as destination:
        source.backup(destination)
    if os.name != "nt":
        os.chmod(backup, stat.S_IMODE(path.stat().st_mode) & 0o600)
    return backup


def rename_protobuf_metadata(message: Message) -> bool:
    """Rename metadata keys throughout an A2A protobuf without changing user text."""
    changed = False
    metadata = getattr(message, "metadata", None)
    if metadata is not None:
        for key in list(metadata):
            if key.startswith(OLD_PREFIX):
                replacement = NEW_PREFIX + key[len(OLD_PREFIX):]
                if replacement in metadata:
                    raise ValueError(f"Conflicting stored metadata keys: {key}, {replacement}")
                metadata[replacement] = metadata[key]
                del metadata[key]
                changed = True
    for field, value in message.ListFields():
        if field.type != field.TYPE_MESSAGE or field.name == "metadata":
            continue
        if field.is_repeated:
            for child in value:
                if isinstance(child, Message):
                    changed = rename_protobuf_metadata(child) or changed
        elif isinstance(value, Message):
            changed = rename_protobuf_metadata(value) or changed
    return changed


def rename_json_metadata(raw: str) -> str:
    """Rename only dictionary keys, preserving unrelated content and user text."""
    data = json.loads(raw)

    def visit(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                renamed = NEW_PREFIX + key[len(OLD_PREFIX):] if key.startswith(OLD_PREFIX) else key
                if renamed in result:
                    raise ValueError(f"Conflicting stored metadata keys: {key}, {renamed}")
                result[renamed] = visit(item)
            return result
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    updated = visit(data)
    return json.dumps(updated, ensure_ascii=False) if updated != data else raw
