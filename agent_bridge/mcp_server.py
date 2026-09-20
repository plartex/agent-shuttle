"""MCP tools that delegate to the A2A agents."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from .client import BridgeClient
from .evaluation import EvaluationService, EvaluationTarget, load_code_smells_profile
from .evaluation.providers import agent_bridge_provider_from_name


mcp = FastMCP(
    "Codex Antigravity A2A bridge",
    instructions=(
        "Use ask_antigravity to delegate a task to Antigravity and ask_codex to delegate a task to Codex. "
        "Use evaluate for batched code-smell checks of a snippet, file, or project. "
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
    }


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


@mcp.tool()
async def evaluate(
    target_type: str,
    target: str,
    profile: str = "code_smells",
    rules: list[str] | None = None,
    provider: str = "agent-bridge:codex",
    model: str | None = None,
    reasoning_effort: str | None = None,
    batch_size: int = 10,
    language: str | None = None,
) -> dict:
    """Evaluate a code snippet, file, or project with batched LLM checks and return structured results."""
    if profile != "code_smells":
        raise ValueError(f"Unknown evaluation profile: {profile}")
    if target_type == "snippet":
        evaluation_target = EvaluationTarget.snippet(target, language=language)
    elif target_type == "file":
        evaluation_target = EvaluationTarget.file(target)
    elif target_type == "project":
        evaluation_target = EvaluationTarget.project(target)
    else:
        raise ValueError("target_type must be snippet, file, or project")
    evaluation_profile = load_code_smells_profile(rule_ids=rules)
    agent_provider = agent_bridge_provider_from_name(provider)
    report = await EvaluationService(agent_provider).evaluate(
        evaluation_target,
        evaluation_profile,
        model=model,
        reasoning_effort=reasoning_effort,
        batch_size=batch_size,
    )
    return report.to_dict()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
