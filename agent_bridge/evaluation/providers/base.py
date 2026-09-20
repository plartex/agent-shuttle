"""Agent transport abstraction used by the LLM check executor."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..models import ProviderDescriptor


@dataclass(frozen=True)
class ProviderCapabilities:
    file_targets: bool = True
    dynamic_workspace: bool = False
    read_only: bool = False
    structured_output: bool = False


class AgentProvider(Protocol):
    capabilities: ProviderCapabilities

    def descriptor(
        self,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ProviderDescriptor: ...

    async def run(
        self,
        prompt: str,
        *,
        workspace: Path | None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> str: ...
