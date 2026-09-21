"""Agent Bridge transport provider."""

from __future__ import annotations

import os
from pathlib import Path

from ...client import BridgeClient
from ..models import ProviderDescriptor
from .base import ProviderCapabilities, ProviderResponse


class AgentBridgeProvider:
    def __init__(self, peer_url: str, agent: str, timeout_seconds: float = 1800):
        if not peer_url.strip():
            raise ValueError("peer_url must be nonempty")
        if not agent.strip():
            raise ValueError("agent must be nonempty")
        self.peer_url = peer_url.rstrip("/")
        self.agent = agent.strip()
        self.client = BridgeClient(timeout_seconds=timeout_seconds)
        self.capabilities = ProviderCapabilities(
            file_targets=True,
            dynamic_workspace=False,
            read_only=self.agent.lower() == "codex",
            structured_output=False,
        )

    def descriptor(
        self,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="agent_bridge",
            agent=self.agent,
            model=model,
            reasoning_effort=reasoning_effort,
            read_only=self.capabilities.read_only,
        )

    async def run(
        self,
        prompt: str,
        *,
        workspace: Path | None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ProviderResponse:
        # The current bridge server owns its workspace. The absolute target path in
        # the prompt scopes the evaluation; workspace is retained for future
        # dynamic-workspace support.
        result = await self.client.ask(
            self.peer_url,
            prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            read_only=self.capabilities.read_only,
        )
        if result.state not in {"TASK_STATE_COMPLETED", "message"}:
            raise RuntimeError(f"Agent Bridge task ended in {result.state}: {result.text}")
        return ProviderResponse(result.text, result.usage)


def agent_bridge_provider_from_name(name: str) -> AgentBridgeProvider:
    normalized = name.strip().lower()
    aliases = {
        "codex": ("codex", "BRIDGE_CODEX_URL"),
        "agent-bridge:codex": ("codex", "BRIDGE_CODEX_URL"),
        "antigravity": ("antigravity", "BRIDGE_ANTIGRAVITY_URL"),
        "agent-bridge:antigravity": ("antigravity", "BRIDGE_ANTIGRAVITY_URL"),
    }
    if normalized not in aliases:
        raise ValueError(f"Unknown provider: {name}")
    agent, env_name = aliases[normalized]
    url = os.environ.get(env_name)
    if not url:
        raise RuntimeError(f"Set {env_name} to the local A2A server URL")
    return AgentBridgeProvider(url, agent)
