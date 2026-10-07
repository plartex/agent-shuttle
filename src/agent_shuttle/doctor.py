"""Local runtime diagnostics with an explicitly opt-in model turn."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .discovery import discover_harnesses
from .profiles import AgentProfile
from .registry import build_builtin, build_profile


BUILTINS = ("codex", "antigravity", "opencode", "claude_code")
PROBE_TIMEOUT = 50
SMOKE_TIMEOUT = 120
SMOKE_PROMPT = "Reply with OK. Do not use tools."


class DoctorConfigError(ValueError):
    """An invocation or registered profile cannot be interpreted."""


def _mapping() -> dict:
    try:
        mapping = json.loads(os.environ.get("AGENT_SHUTTLE_AGENTS_JSON", "{}"))
    except json.JSONDecodeError as exc:
        raise DoctorConfigError("AGENT_SHUTTLE_AGENTS_JSON must be a JSON object") from exc
    if not isinstance(mapping, dict):
        raise DoctorConfigError("AGENT_SHUTTLE_AGENTS_JSON must be a JSON object")
    for name in mapping:
        if not isinstance(name, str) or not name:
            raise DoctorConfigError("AGENT_SHUTTLE_AGENTS_JSON keys must be nonempty agent IDs")
        _entry(mapping, name)
    return mapping


def _entry(mapping: dict, agent_id: str) -> dict:
    value = mapping.get(agent_id, {})
    if isinstance(value, str):
        value = {"url": value}
    if not isinstance(value, dict):
        raise DoctorConfigError(f"{agent_id}: agent configuration must be an object or URL")
    return value


def _profile(path: Path, workspace: str | None = None) -> AgentProfile:
    try:
        return AgentProfile.from_file(
            path, workspace_override=Path(workspace) if workspace else None,
        )
    except (OSError, ValueError, TypeError) as exc:
        raise DoctorConfigError(f"{path}: {exc}") from exc


def _check(target: str, status: str, level: str, message: str, action: str) -> dict:
    return {"target": target, "status": status, "level": level,
            "message": message, "action": action, "turn_verified": level == "turn" and status == "OK"}


def _error(exc: Exception) -> str:
    """Keep raw process stderr and possible credentials out of the report."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timed out"
    if isinstance(exc, FileNotFoundError):
        return "command not found"
    if "Authentication" in type(exc).__name__:
        return "authentication failed"
    return f"{type(exc).__name__}; inspect runtime logs for details"


async def _smoke(target: str, backend, policy: str) -> dict:
    try:
        response = await asyncio.wait_for(
            backend.run(SMOKE_PROMPT, tool_policy=policy), SMOKE_TIMEOUT,
        )
        answer = getattr(response, "text", response)
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("agent returned no text")
        result = _check(target, "OK", "turn", "A model turn completed.", "No action needed.")
        result["output"] = answer
        return result
    except Exception as exc:
        return _check(target, "FAIL", "turn", f"Model turn failed: {_error(exc)}",
                      "Check authentication, model access and runtime logs; retry --smoke.")


async def _builtin(target: str, found: dict[str, str], *, explicit: bool, smoke: bool,
                   workspace: Path) -> dict:
    executable = found.get(target)
    if executable is None:
        status = "FAIL" if explicit else "SKIP"
        return _check(target, status, "install", "Runtime is not installed.",
                      "Install the runtime or configure its command; run discover to verify it.")
    if target in {"opencode", "claude_code"}:
        if smoke:
            return _check(target, "FAIL", "install", "A profile is required for a safe model turn.",
                          "Configure a JSON profile and run doctor --profile PATH --smoke.")
        return _check(target, "WARN", "install", "Command found; account and model are unverified.",
                      "Configure a JSON profile to check runtime readiness.")
    try:
        backend, info = build_builtin(target, workspace, command=executable)
        if target == "antigravity":
            await asyncio.wait_for(info.check_ready(), PROBE_TIMEOUT)
            message = "Command and model catalog responded; a model turn is unverified."
        else:
            await asyncio.wait_for(info.fetch(capabilities=True, usage=False), PROBE_TIMEOUT)
            message = "Codex App Server responded; account access and a model turn are unverified."
        if smoke:
            return await _smoke(target, backend, "read_only" if target == "codex" else "no_tools")
        return _check(target, "OK", "connect", message,
                      f"Run doctor {target} --smoke to verify a model turn.")
    except Exception as exc:
        return _check(target, "FAIL", "connect", f"Connection check failed: {_error(exc)}",
                      "Check the runtime installation and authentication; retry doctor.")


async def _configured(profile: AgentProfile, *, smoke: bool, target: str | None = None) -> dict:
    target = target or profile.id
    backend, info = build_profile(profile)
    result: dict
    try:
        if profile.runtime == "acp":
            discovery = await asyncio.wait_for(backend.runtime.discover(), PROBE_TIMEOUT)
            if not discovery["command_found"]:
                result = _check(target, "FAIL", "install", "ACP command was not found.",
                                "Correct the profile command and retry doctor.")
            else:
                result = {}
        else:
            result = {}
        if not result:
            await asyncio.wait_for(info.fetch(capabilities=True, usage=False), PROBE_TIMEOUT)
            if smoke:
                if profile.runtime == "acp":
                    result = _check(target, "FAIL", "turn",
                                    "ACP cannot enforce no_tools or read_only for a safe smoke turn.",
                                    "Use an externally isolated ACP process before testing a model turn.")
                else:
                    result = await _smoke(target, backend, "no_tools")
            else:
                detail = ("ACP handshake and session creation succeeded" if profile.runtime == "acp"
                          else "Runtime responded")
                result = _check(target, "OK", "connect", f"{detail}; a model turn is unverified.",
                                ("ACP needs external isolation for a safe smoke turn." if profile.runtime == "acp"
                                 else f"Run doctor --profile PATH --smoke to verify {target}."))
    except Exception as exc:
        result = _check(target, "FAIL", "connect", f"Connection check failed: {_error(exc)}",
                        "Check profile, runtime command and authentication; retry doctor.")
    finally:
        try:
            await asyncio.wait_for(backend.close(), 10)
        except Exception as exc:
            result = _check(target, "FAIL", "connect", f"Runtime cleanup failed: {_error(exc)}",
                            "Stop the local runtime process and inspect runtime logs.")
    return result


async def diagnose(agent_id: str | None = None, *, profile_path: Path | None = None,
                   smoke: bool = False) -> dict:
    """Return a stable, redacted report; never send a prompt unless smoke is true."""
    if profile_path is not None and agent_id is not None:
        raise DoctorConfigError("Use either agent_id or --profile, not both")
    if smoke and agent_id is None and profile_path is None:
        raise DoctorConfigError("--smoke requires one agent_id or --profile")
    mapping = _mapping()
    found = discover_harnesses()
    checks: list[dict] = []
    if profile_path is not None:
        checks.append(await _configured(_profile(profile_path), smoke=smoke))
    else:
        targets = [agent_id] if agent_id else list(BUILTINS) + [
            name for name, value in mapping.items()
            if name not in BUILTINS and isinstance(value, dict) and "profile" in value
        ]
        for target in targets:
            entry = _entry(mapping, target)
            if "profile" in entry:
                path = entry["profile"]
                if not isinstance(path, str) or not path:
                    raise DoctorConfigError(f"{target}: profile must be a nonempty path")
                profile = _profile(Path(path), entry.get("workspace"))
                checks.append(await _configured(profile, smoke=smoke, target=target))
            elif target in BUILTINS:
                if "url" in entry:
                    checks.append(_check(target, "FAIL" if smoke else "WARN", "install",
                                         "URL-only entry is not a local runtime.",
                                         "Use agent-shuttle info URL to inspect the running server."))
                    continue
                workspace = Path(entry.get("workspace") or Path.cwd())
                try:
                    workspace = workspace.resolve(strict=True)
                    if not workspace.is_dir():
                        raise ValueError("workspace is not a directory")
                except (OSError, ValueError) as exc:
                    raise DoctorConfigError(f"{target}: invalid workspace: {exc}") from exc
                checks.append(await _builtin(target, found, explicit=agent_id is not None,
                                             smoke=smoke, workspace=workspace))
            else:
                raise DoctorConfigError(f"{target}: no local runtime profile is registered")
    status = "FAIL" if any(item["status"] == "FAIL" for item in checks) else (
        "WARN" if not any(item["status"] == "OK" for item in checks)
        or any(item["status"] == "WARN" for item in checks) else "OK"
    )
    return {"schema_version": 1, "status": status, "checks": checks}


def format_report(report: dict) -> str:
    return "\n".join(
        f"{item['status']} {item['target']} [{item['level']}]: {item['message']} "
        f"Next: {item['action']}"
        + (f" Output: {json.dumps(item['output'], ensure_ascii=False)}" if "output" in item else "")
        for item in report["checks"]
    )
