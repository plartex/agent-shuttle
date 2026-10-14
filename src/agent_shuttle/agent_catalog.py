"""Side-effect-free inventory; availability is not authentication or readiness."""

import json
import os
import re
import shutil
from pathlib import Path

from .discovery import discover_harnesses
from .profiles import AgentProfile

BUILTINS = {"codex": "Codex", "antigravity": "Antigravity",
            "opencode": "OpenCode", "claude_code": "Claude Code"}


def inventory() -> dict:
    found = discover_harnesses()
    try:
        mapping = json.loads(os.environ.get("AGENT_SHUTTLE_AGENTS_JSON", "{}"))
        if not isinstance(mapping, dict):
            raise ValueError("agent configuration must be an object")
    except (ValueError, TypeError):
        return {"agents": [_item(key, title, key, key in found, "default")
                           for key, title in BUILTINS.items()],
                "configuration_error": "AGENT_SHUTTLE_AGENTS_JSON must be a JSON object"}
    items = []
    for key in dict.fromkeys([*BUILTINS, *mapping]):
        title = BUILTINS.get(key, key)
        entry = mapping.get(key, {})
        runtime = key
        available = key in found
        status = "default" if key in BUILTINS and key not in mapping else "configured"
        error = None
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                raise ValueError("invalid agent_id")
            if isinstance(entry, str):
                from .mcp_server import _local_url
                _local_url(entry)
                status = "configured_remote"
                available = None
            elif isinstance(entry, dict):
                runtime = entry.get("harness", "acp" if entry.get("profile") and key not in BUILTINS else key)
                if runtime not in {*BUILTINS, "acp"}:
                    raise ValueError("unsupported harness")
                available = runtime in found
                if entry.get("command"):
                    available = runtime in discover_harnesses({runtime: entry["command"]})
                if entry.get("profile"):
                    profile = AgentProfile.from_file(Path(entry["profile"]))
                    if profile.runtime != runtime:
                        raise ValueError("profile runtime differs from harness")
                    command = profile.command[0] if runtime == "acp" else profile.runtime_command or runtime.replace("claude_code", "claude")
                    available = bool(shutil.which(command) or Path(command).is_file())
                    status = "configured"
                elif runtime == "acp":
                    raise ValueError("ACP requires a profile")
                if entry.get("url"):
                    from .mcp_server import _local_url
                    _local_url(entry["url"])
            else:
                raise ValueError("configuration must be an object or local URL")
        except (OSError, TypeError, ValueError, KeyError):
            status, error = "invalid", "Invalid agent configuration; check harness, profile and local URL."
        item = _item(key, title, runtime, available, status)
        if error:
            item["error"] = error
        items.append(item)
    return {"agents": items, "configuration_error": None}


def _item(key, title, runtime, available, status):
    return {"agent_id": key, "title": title, "harness": runtime,
            "command_available": available, "configuration_status": status,
            "authentication": "unverified", "readiness": "unverified"}
