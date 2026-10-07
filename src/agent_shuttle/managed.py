"""Reusable lifecycle for an existing or temporarily launched local Shuttle peer."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import socket
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlparse

from .client import ShuttleClient
from .discovery import discover_harnesses
from .profiles import AgentProfile
from .local_auth import PeerAuthenticationError
from .process_lifecycle import owns_pid, spawn_options, stop_sync_process


_BACKENDS = {
    "codex": {"codex_app_server"},
    "antigravity": {"agy_cli", "antigravity_sdk"},
    "opencode": {"opencode"},
    "claude_code": {"claude_code"},
    "acp": {"acp"},
}


def _trace(stage: str) -> None:
    if os.environ.get("AGENT_SHUTTLE_DEBUG") == "1":
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[agent-shuttle] {stamp} managed: {stage}", file=sys.stderr, flush=True)


async def _stop_process_tree(process: subprocess.Popen) -> None:
    await stop_sync_process(process)


@dataclass(frozen=True)
class HarnessLaunch:
    """How to connect to a Shuttle peer, starting one only when needed.

    ``profile_path`` selects a user-owned OpenCode/Claude Code profile. Without
    one, those runtimes receive a temporary Ollama profile for ``model`` with
    ``tool_policy`` as its maximum (or no tools by default). For Antigravity,
    ``full_access`` enables the CLI's all-permissions mode automatically.
    ``start_if_missing=False`` is appropriate for an explicitly supplied URL.
    """

    name: str
    url: str
    workspace: Path
    model: str | None = None
    command: str | None = None
    profile_path: Path | None = None
    ollama_url: str = "http://127.0.0.1:11434"
    log_path: Path | None = None
    start_if_missing: bool = True
    tool_policy: str | None = None
    agy_dangerously_skip_permissions: bool = False
    agy_turn_timeout_seconds: float = 300
    task_db: Path | None = None


@dataclass(frozen=True)
class ShuttleConnection:
    url: str
    started: bool
    log_path: Path | None = None


class HarnessConfigurationMismatch(ValueError):
    """A live peer cannot serve the requested workspace or access policy."""


def _verify_backend(name: str, url: str, info: dict) -> None:
    backend = info.get("backend")
    if backend not in _BACKENDS[name]:
        raise HarnessConfigurationMismatch(f"{url} serves backend {backend!r}, not {name!r}")


def _verify_connection(launch: HarnessLaunch, info: dict) -> None:
    _verify_backend(launch.name, launch.url, info)
    expected = launch.workspace.resolve(strict=True)
    reported = info.get("workspace")
    if not isinstance(reported, str) or not Path(reported).is_absolute():
        raise HarnessConfigurationMismatch(f"{launch.url} did not report a valid workspace")
    try:
        actual = Path(reported).resolve(strict=True)
    except OSError as exc:
        raise HarnessConfigurationMismatch(f"{launch.url} reported an inaccessible workspace") from exc
    if not actual.samefile(expected):
        raise HarnessConfigurationMismatch(f"{launch.url} workspace {actual} does not match {expected}")
    pid = info.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise HarnessConfigurationMismatch(f"{launch.url} did not report a valid process ID")
    if launch.task_db is not None:
        reported_db = info.get("task_db_path")
        if (info.get("task_storage") != "sqlite" or not isinstance(reported_db, str)
                or not Path(reported_db).is_file()
                or not Path(reported_db).samefile(launch.task_db)):
            raise HarnessConfigurationMismatch(f"{launch.url} does not use the requested persistent task database")
    advisory = launch.name == "acp" and info.get("tool_policy_enforcement") == "advisory"
    if launch.tool_policy == "read_only" and info.get("read_only_tools") is not True and not advisory:
        raise HarnessConfigurationMismatch(f"{launch.url} cannot confirm read-only tools")
    policies = info.get("advisory_tool_policies") if advisory else info.get("supported_tool_policies")
    if launch.tool_policy is not None and isinstance(policies, list) and launch.tool_policy not in policies:
        raise HarnessConfigurationMismatch(f"{launch.url} cannot enforce {launch.tool_policy}")
    if launch.name == "antigravity":
        expected_mode = (
            "all" if launch.tool_policy == "full_access" or launch.agy_dangerously_skip_permissions
            else "settings"
        )
        if info.get("agy_permission_mode") != expected_mode:
            raise HarnessConfigurationMismatch(f"{launch.url} Antigravity permission mode does not match {expected_mode!r}")
        if info.get("agy_turn_timeout_seconds") != launch.agy_turn_timeout_seconds:
            raise HarnessConfigurationMismatch(f"{launch.url} Antigravity turn timeout does not match")


def _local_port(url: str) -> int:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Temporary Shuttle servers require an HTTP loopback URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Temporary Shuttle URL has an invalid port") from exc
    if port is None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Temporary Shuttle URL must contain only a host and port")
    return port


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@asynccontextmanager
async def connect_harness(
    launch: HarnessLaunch, *, client: ShuttleClient | None = None,
) -> AsyncIterator[ShuttleConnection]:
    """Reuse a matching peer or supervise a temporary one for the context."""
    if launch.name not in _BACKENDS:
        raise ValueError(f"Unknown harness {launch.name!r}")
    if launch.tool_policy not in {None, "no_tools", "read_only", "workspace_write", "full_access"}:
        raise ValueError("Unknown temporary harness tool_policy")
    if not isinstance(launch.agy_dangerously_skip_permissions, bool):
        raise ValueError("agy_dangerously_skip_permissions must be a boolean")
    if launch.agy_dangerously_skip_permissions and launch.name != "antigravity":
        raise ValueError("agy_dangerously_skip_permissions requires the antigravity harness")
    if launch.agy_turn_timeout_seconds != 300 and launch.name != "antigravity":
        raise ValueError("agy_turn_timeout_seconds requires the antigravity harness")
    if launch.name == "antigravity":
        if (not isinstance(launch.agy_turn_timeout_seconds, (int, float))
                or isinstance(launch.agy_turn_timeout_seconds, bool)
                or not 0 < launch.agy_turn_timeout_seconds < float("inf")):
            raise ValueError("agy_turn_timeout_seconds must be positive and finite")
    client = client or ShuttleClient()
    identify = getattr(client, "identity", None) or client.capabilities
    _trace("checking existing server")
    try:
        _verify_connection(launch, await identify(launch.url))
    except PeerAuthenticationError as exc:
        if not launch.start_if_missing:
            raise HarnessConfigurationMismatch(str(exc)) from exc
        if _port_in_use(_local_port(launch.url)):
            launch = replace(launch, url=f"http://127.0.0.1:{_available_port()}")
            _trace(f"untrusted occupied port; using {launch.url}")
    except ValueError:
        raise
    except Exception as exc:
        _trace(f"existing server unavailable ({type(exc).__name__})")
        if not launch.start_if_missing:
            raise RuntimeError(f"Shuttle at {launch.url} is unavailable: {exc}") from exc
    else:
        yield ShuttleConnection(launch.url, started=False)
        return

    port = _local_port(launch.url)
    workspace = launch.workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace must be a directory")
    profile_path = launch.profile_path.resolve(strict=True) if launch.profile_path else None
    if profile_path is not None and launch.name not in {"opencode", "claude_code", "acp"}:
        raise ValueError("Profiles are supported only for OpenCode, Claude Code and ACP")
    if profile_path is not None and launch.tool_policy is not None:
        AgentProfile.from_file(profile_path, workspace_override=workspace).resolve(
            launch.model, None, launch.tool_policy,
        )
    command = launch.command or discover_harnesses().get(launch.name)
    _trace(f"discovered command={bool(command)}")
    if command is None and profile_path is None:
        raise RuntimeError(f"{launch.name} is not installed; provide command or a running Shuttle URL")

    with tempfile.TemporaryDirectory(prefix="agent-shuttle-") as temporary:
        if launch.name in {"opencode", "claude_code"} and profile_path is None:
            if not launch.model:
                raise ValueError("model is required for a temporary Ollama profile")
            profile_path = Path(temporary) / "profile.json"
            profile = {
                "id": launch.name, "runtime": launch.name, "provider": "ollama",
                "workspace": str(workspace), "endpoint": launch.ollama_url,
                "default_model": launch.model, "allowed_models": [launch.model],
                "runtime_command": command, "max_tool_policy": launch.tool_policy or "no_tools",
            }
            if launch.name == "opencode":
                profile["reasoning_efforts"] = ["none"]
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
        argv = [sys.executable, "-m", "agent_shuttle.cli", "serve",
                "profile" if profile_path else launch.name,
                "--port", str(port), "--workspace", str(workspace)]
        if profile_path:
            argv.extend(["--profile", str(profile_path)])
        if launch.task_db is not None:
            argv.extend(["--task-db", str(launch.task_db)])
        if launch.name == "antigravity":
            argv.extend(["--agy-command", command])
            argv.extend(["--agy-turn-timeout-seconds", f"{launch.agy_turn_timeout_seconds:g}"])
            if launch.tool_policy == "full_access" or launch.agy_dangerously_skip_permissions:
                argv.append("--agy-dangerously-skip-permissions")
        log_path = launch.log_path or Path(temporary) / "shuttle.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log:
            _trace(f"spawning temporary server executable={sys.executable} log={log_path}")
            process = subprocess.Popen(
                argv, cwd=workspace, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
                **spawn_options(),
            )
            _trace(f"temporary server pid={process.pid}")
            try:
                last_error: Exception | None = None
                # Antigravity checks its signed-in model catalog before listening;
                # that check may take up to 45 seconds on a healthy account.
                attempts = 120 if launch.name == "antigravity" else 60
                for _ in range(attempts):
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"{launch.name} Shuttle exited ({process.returncode}); "
                            f"log tail: {log_path.read_text(encoding='utf-8', errors='replace')[-2000:]}"
                        )
                    try:
                        info = await identify(launch.url)
                        _verify_connection(launch, info)
                        if not owns_pid(process.pid, info["pid"]):
                            raise HarnessConfigurationMismatch(
                                f"{launch.url} belongs to PID {info['pid']}, not launched PID {process.pid}"
                            )
                        _trace("temporary server identity verified")
                        break
                    except ValueError:
                        raise
                    except Exception as exc:
                        last_error = exc
                        if os.environ.get("AGENT_SHUTTLE_DEBUG") == "1" and _ < 3:
                            _trace(f"identity attempt {_ + 1} failed ({type(exc).__name__}: {str(exc)[:200]})")
                        await asyncio.sleep(0.5)
                else:
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                    raise TimeoutError(
                        f"{launch.name} Shuttle did not become ready: {last_error}; log tail: {tail}"
                    )
                yield ShuttleConnection(launch.url, started=True, log_path=log_path)
            finally:
                await _stop_process_tree(process)
