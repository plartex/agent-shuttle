"""MCP tools that delegate to the A2A agents."""

from __future__ import annotations

import os
import json
import re
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

from .client import BridgeClient


mcp = FastMCP(
    "Agent Bridge",
    instructions=(
        "Use ask_agent for configured OpenCode, Claude Code, or other agent profiles. "
        "The legacy ask_antigravity and ask_codex tools remain available. "
        "Use get_antigravity_info or get_codex_info to check current models, efforts and account quotas. "
        "Each call starts a new remote task. When the user names a model for the remote agent, "
        "pass that model ID exactly in the optional model parameter. When the user names a "
        "reasoning effort, pass it exactly in reasoning_effort. "
        "Return the remote result to the user."
    ),
)


async def _ask(
    env_name: str,
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    url = os.environ.get(env_name)
    if not url:
        raise RuntimeError(f"Set {env_name} to the local A2A server URL")
    result = await BridgeClient().ask(
        url,
        prompt,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    return {
        "task_id": result.task_id,
        "context_id": result.context_id,
        "state": result.state,
        "text": result.text,
        "usage": result.usage,
        "details": result.details,
    }


def _agent_url(agent_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id):
        raise ValueError("agent_id must contain only letters, digits, hyphen, or underscore")
    configured = os.environ.get("BRIDGE_AGENTS_JSON", "{}")
    try:
        mapping = json.loads(configured)
    except json.JSONDecodeError as exc:
        raise ValueError("BRIDGE_AGENTS_JSON must be a JSON object") from exc
    if not isinstance(mapping, dict):
        raise ValueError("BRIDGE_AGENTS_JSON must be a JSON object")
    url = mapping.get(agent_id)
    if url is None:
        raise ValueError(f"Unknown agent profile {agent_id!r} in BRIDGE_AGENTS_JSON")
    if not isinstance(url, str):
        raise ValueError("Agent URL must be a string")
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Agent URL must use local HTTP loopback")
    return url.rstrip("/")


@mcp.tool()
async def ask_agent(
    agent_id: str,
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    tool_policy: str | None = None,
) -> dict:
    """Ask a configured agent profile from BRIDGE_AGENTS_JSON."""
    result = await BridgeClient().ask(
        _agent_url(agent_id), prompt, model=model,
        reasoning_effort=reasoning_effort, tool_policy=tool_policy,
    )
    return {
        "task_id": result.task_id, "context_id": result.context_id,
        "state": result.state, "text": result.text,
        "usage": result.usage, "details": result.details,
    }


@mcp.tool()
async def get_agent_info(agent_id: str) -> dict:
    """Read capabilities and quota information for a configured agent profile."""
    return await BridgeClient().info(_agent_url(agent_id))


@mcp.tool()
async def ask_antigravity(
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    """Delegate to Antigravity. model and reasoning_effort map to agy launch flags."""
    return await _ask("BRIDGE_ANTIGRAVITY_URL", prompt, model, reasoning_effort)


@mcp.tool()
async def ask_codex(
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    """Delegate to Codex. model and reasoning_effort select its thread settings."""
    return await _ask("BRIDGE_CODEX_URL", prompt, model, reasoning_effort)


@mcp.tool()
async def get_antigravity_info() -> dict:
    """Read Antigravity's current models, effort options and account quota without an agent turn."""
    url = os.environ.get("BRIDGE_ANTIGRAVITY_URL")
    if not url:
        raise RuntimeError("Set BRIDGE_ANTIGRAVITY_URL to the local A2A server URL")
    return await BridgeClient().info(url)


@mcp.tool()
async def get_codex_info() -> dict:
    """Read Codex's current models, supported efforts and account quota without an agent turn."""
    url = os.environ.get("BRIDGE_CODEX_URL")
    if not url:
        raise RuntimeError("Set BRIDGE_CODEX_URL to the local A2A server URL")
    return await BridgeClient().info(url)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
