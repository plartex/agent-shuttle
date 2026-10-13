"""Explicit, reversible MCP configuration and transport diagnostics."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
import uuid
from pathlib import Path

import tomlkit
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .presentation import VERSION

REQUIRED_TOOLS = {"ask_agent", "submit_task", "get_agent_info", "list_agents", "get_result"}


class McpDiagnosticError(ValueError):
    """A safe, structured diagnostic without external configuration or stderr."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def parse(text: str, format: str):
    if format == "codex-toml":
        return tomlkit.parse(text)
    if format == "mcp-json":
        document = json.loads(text) if text.strip() else {}
        if not isinstance(document, dict):
            raise ValueError("MCP configuration must be an object")
        return document
    raise ValueError("Unsupported configuration format")


def _keys(format):
    return ("mcp_servers", "agent_shuttle") if format == "codex-toml" else ("mcpServers", "agent-shuttle")


def entry(document, format):
    group, name = _keys(format)
    section = document.get(group, {})
    if not isinstance(section, dict):
        raise ValueError("MCP server section must be an object")
    value = section.get(name)
    if value is None:
        candidate = section.get("MCP_DOCKER", {})
        arguments = candidate.get("args", []) if isinstance(candidate, dict) else []
        if isinstance(arguments, list) and "agent_shuttle" in arguments and "gateway" in arguments:
            value = candidate
    if not isinstance(value, dict) or not value.get("command"):
        raise McpDiagnosticError("not_configured", "Agent Shuttle MCP is not configured")
    if value.get("enabled") is False or value.get("disabled") is True:
        raise McpDiagnosticError("disabled", "Agent Shuttle MCP entry is disabled")
    return value


def configured_text(text: str, format: str, workspace: Path) -> str:
    document = parse(text, format)
    group, name = _keys(format)
    if group not in document:
        document[group] = tomlkit.table() if format == "codex-toml" else {}
    if not isinstance(document[group], dict):
        raise ValueError("MCP server section must be an object")
    servers = document[group]
    if name not in servers:
        servers[name] = tomlkit.table() if format == "codex-toml" else {}
    if not isinstance(servers[name], dict):
        raise ValueError("Agent Shuttle entry must be an object")
    value = servers[name]
    if "enabled" in value:
        value["enabled"] = True
    if "disabled" in value:
        value["disabled"] = False
    value["command"] = sys.executable
    value["args"] = ["-m", "agent_shuttle.mcp_server"]
    if "env" not in value:
        value["env"] = tomlkit.table() if format == "codex-toml" else {}
    if not isinstance(value["env"], dict):
        raise ValueError("Agent Shuttle environment must be an object")
    value["env"]["AGENT_SHUTTLE_WORKSPACE"] = str(workspace)
    if format == "codex-toml":
        value["tool_timeout_sec"] = 1800
        forwarded = list(value.get("env_vars", []))
        if "AGENT_SHUTTLE_PARENT_CONTEXT" not in forwarded:
            forwarded.append("AGENT_SHUTTLE_PARENT_CONTEXT")
        value["env_vars"] = forwarded
        return tomlkit.dumps(document)
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


async def probe(value: dict) -> dict:
    command = value["command"]
    args = value.get("args", [])
    configured_env = value.get("env", {})
    if not isinstance(command, str) or not isinstance(args, list) or any(not isinstance(x, str) for x in args):
        raise ValueError("MCP command and arguments must be strings")
    if not isinstance(configured_env, dict) or any(not isinstance(x, str) for x in configured_env.values()):
        raise ValueError("MCP environment values must be strings")
    env = {**os.environ, **configured_env}
    # A config cannot turn an inherited worker into a coordinator.
    from .runtime_context import is_worker_context, worker_env
    if is_worker_context():
        env = worker_env(env)
    async with asyncio.timeout(20):
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
            async with stdio_client(StdioServerParameters(command=command, args=args, env=env), errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialized = await session.initialize()
                    tools = (await session.list_tools()).tools
                    names = {tool.name for tool in tools}
                    missing = REQUIRED_TOOLS - names
                    if missing:
                        raise McpDiagnosticError("missing_tools", "MCP server is missing required Shuttle tools")
                    if initialized.serverInfo.name not in {"Agent Shuttle", "Agent Shuttle + Docker MCP"}:
                        raise McpDiagnosticError("identity_mismatch", "The configured command did not start Agent Shuttle")
                    if initialized.serverInfo.version != VERSION:
                        raise McpDiagnosticError("version_mismatch", "The responding MCP version differs from the installed package; update its launch command")
                    result = await session.call_tool("list_agents", {})
                    if result.isError:
                        raise McpDiagnosticError("inventory_failed", "MCP inventory call failed")
                    data = result.structuredContent
                    if data is None:
                        data = json.loads(next((item.text for item in result.content if item.type == "text"), "{}"))
                    if not isinstance(data, dict) or not isinstance(data.get("agents"), list) or any(
                        not isinstance(item, dict) for item in data["agents"]):
                        raise McpDiagnosticError("inventory_invalid", "MCP inventory returned an invalid result")
                    needs_configuration = bool(data.get("configuration_error")) or any(
                        item.get("configuration_status") == "invalid" for item in data.get("agents", []))
                    return {"name": initialized.serverInfo.name, "version": initialized.serverInfo.version,
                            "tools": sorted(names), "harness_readiness": "unverified",
                            "harness_configuration": "requires_attention" if needs_configuration else "valid"}


def _atomic_write(path: Path, data: bytes, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix=".shuttle-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


async def setup(path: Path, format: str, workspace: Path, apply=False) -> dict:
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace must be an existing directory")
    path = path.absolute()
    if path.is_symlink():
        raise ValueError("Refusing to replace a symlink configuration")
    existed = path.exists()
    before = path.read_bytes() if existed else b""
    text = before.decode("utf-8-sig")
    updated = configured_text(text, format, workspace).encode("utf-8")
    report = {"config": str(path), "changed": updated != before, "applied": False,
              "command": sys.executable, "args": ["-m", "agent_shuttle.mcp_server"],
              "workspace": str(workspace), "connection": "unverified"}
    if not apply:
        return report
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    mode = stat.S_IMODE(path.stat().st_mode) if existed else 0o600
    if updated != before:
        if (path.read_bytes() if path.exists() else b"") != before:
            raise RuntimeError("Configuration changed during setup; retry")
        if existed:
            backup = path.with_name(path.name + f".shuttle-backup-{uuid.uuid4().hex}")
            _atomic_write(backup, before, mode)
        _atomic_write(path, updated, mode)
    try:
        report["server"] = await probe(entry(parse(updated.decode(), format), format))
    except BaseException:
        # Roll back only our own bytes, preserving any concurrent edit.
        if updated != before and path.read_bytes() == updated:
            if existed:
                _atomic_write(path, before, mode)
            else:
                path.unlink()
        raise
    report.update(applied=True, connection="verified")
    if backup:
        report["backup"] = str(backup)
    return report


async def diagnose_mcp(path: Path, format: str) -> dict:
    report = {"status": "FAIL", "package_version": VERSION, "config": str(path.absolute()),
              "connection": "unverified", "harness_readiness": "unverified", "stage": "configuration"}
    if not path.is_file():
        report.update(error_code="config_missing", error="MCP configuration file is missing")
        return report
    try:
        value = entry(parse(path.read_text(encoding="utf-8-sig"), format), format)
        command = value["command"]
        report["stage"] = "command"
        if not isinstance(command, str) or not (shutil.which(command) or Path(command).is_file()):
            report["error"] = "MCP launch command is missing"
            report["error_code"] = "command_missing"
            return report
        report["stage"] = "handshake"
        report["server"] = await probe(value)
        report.update(status="OK", connection="verified", stage="ready",
                      harness_configuration=report["server"].get("harness_configuration", "unverified"))
    except McpDiagnosticError as exc:
        report.update(error_code=exc.code, error=str(exc))
    except (OSError, ValueError, TypeError, TimeoutError, ExceptionGroup) as exc:
        # AnyIO may wrap the safe probe diagnostic in a task-group exception.
        def diagnostic(error):
            if isinstance(error, McpDiagnosticError):
                return error
            for child in getattr(error, "exceptions", []):
                found = diagnostic(child)
                if found:
                    return found
        safe = diagnostic(exc)
        if safe:
            report.update(error_code=safe.code, error=str(safe))
            return report
        report["error_code"] = "config_invalid" if report["stage"] == "configuration" else "handshake_failed"
        report["error"] = "MCP configuration or handshake failed; check command, environment and installed version"
    return report
