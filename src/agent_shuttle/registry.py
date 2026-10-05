"""Runtime registry: one configured profile becomes one A2A agent."""

from __future__ import annotations

from pathlib import Path

from .acp_runtime import AcpRuntime
from .backends import AntigravityCliBackend, AntigravitySdkBackend, CodexBackend
from .claude_runtime import ClaudeCodeRuntime
from .info import AntigravityCliInfo, AntigravitySdkInfo, CodexInfo
from .opencode_runtime import OpenCodeRuntime
from .profiled import ProfiledBackend, ProfiledInfo
from .profiles import AgentProfile


def _codex(workspace: Path, **kwargs):
    return CodexBackend(workspace), CodexInfo(workspace)


def _antigravity(workspace: Path, *, command="agy", mode="cli", python=None,
                 dangerously_skip_permissions=False, turn_timeout_seconds=300, **kwargs):
    if mode == "sdk":
        return AntigravitySdkBackend(workspace, python), AntigravitySdkInfo()
    return (AntigravityCliBackend(
        workspace, command, dangerously_skip_permissions=dangerously_skip_permissions,
        turn_timeout_seconds=turn_timeout_seconds,
    ), AntigravityCliInfo(workspace, command))


def _profile(runtime_type):
    def create(profile: AgentProfile, **kwargs):
        runtime = runtime_type(profile)
        return ProfiledBackend(profile, runtime), ProfiledInfo(profile, runtime)
    return create


_FACTORIES = {
    "codex": _codex,
    "antigravity": _antigravity,
    "opencode": _profile(OpenCodeRuntime),
    "claude_code": _profile(ClaudeCodeRuntime),
    "acp": _profile(AcpRuntime),
}


def build_runtime(kind: str, configuration: Path | AgentProfile, **kwargs):
    """Single factory boundary for built-in and configured workers."""
    return _FACTORIES[kind](configuration, **kwargs)


def build_builtin(name: str, workspace: Path, **kwargs):
    """Create a legacy named worker through the same registry entry point."""
    return build_runtime(name, workspace, **kwargs)


def build_profile(profile: AgentProfile) -> tuple[ProfiledBackend, ProfiledInfo]:
    return build_runtime(profile.runtime, profile)
