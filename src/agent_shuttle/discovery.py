"""Discover user-installed harness commands without starting them."""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

COMMANDS = {"codex": "codex", "antigravity": "agy", "opencode": "opencode", "claude_code": "claude"}


def _find_command(name: str, command: str | None = None) -> str | None:
    variable = "AGY" if name == "antigravity" else name.upper()
    explicit = command if command is not None else os.environ.get(f"AGENT_SHUTTLE_{variable}_COMMAND")
    if explicit is not None and (not isinstance(explicit, str) or not explicit.strip()):
        raise ValueError(f"{name} command must be a nonempty executable name or path")
    search_path = None
    if name == "codex" and not explicit:
        spec = importlib.util.find_spec("codex_cli_bin")
        bundled = Path(spec.origin).parent.resolve() if spec and spec.origin else None
        search_path = os.pathsep.join(
            entry for entry in os.get_exec_path()
            if not bundled or not Path(entry).resolve().is_relative_to(bundled)
        )
    found = shutil.which(explicit or COMMANDS[name], path=search_path)
    if not found and explicit and Path(explicit).is_file():
        found = str(Path(explicit).resolve())
    if not found and explicit:
        raise FileNotFoundError(f"{name} command not found: {explicit}")
    return str(Path(found).absolute()) if found else None


def discover_harnesses(commands: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve explicit commands, environment overrides, then PATH (no installs)."""
    overrides = commands or {}
    unknown = set(overrides) - COMMANDS.keys()
    if unknown:
        raise ValueError(f"Unknown harnesses: {', '.join(sorted(unknown))}")
    return {name: found for name in COMMANDS if (found := _find_command(name, overrides.get(name)))}


def codex_command(command: str | None = None) -> tuple[str, ...]:
    """Resolve a native executable or the standard Windows npm launcher."""
    found = _find_command("codex", command)
    if not found:
        raise FileNotFoundError("Codex is not installed; add codex to PATH or set AGENT_SHUTTLE_CODEX_COMMAND")
    path = Path(found)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        script = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = path.parent / "node.exe"
        executable = str(node) if node.is_file() else shutil.which("node")
        if not script.is_file() or not executable:
            raise FileNotFoundError("Cannot launch Codex npm shim; configure the Codex executable explicitly")
        return (str(Path(executable).absolute()), str(script))
    return (found,)
