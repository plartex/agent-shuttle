"""Small programmable provider for tests and embedding examples."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Awaitable, Callable

from ..models import ProviderDescriptor
from .base import ProviderCapabilities, ProviderResponse


FakeHandler = Callable[
    [str, Path | None, str | None],
    str | ProviderResponse | Awaitable[str | ProviderResponse],
]


class FakeAgentProvider:
    capabilities = ProviderCapabilities(
        file_targets=True,
        dynamic_workspace=True,
        read_only=True,
        structured_output=True,
    )

    def __init__(self, handler: FakeHandler):
        self.handler = handler
        self.calls: list[tuple[str, Path | None, str | None, str | None]] = []

    def descriptor(
        self,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="fake",
            agent="fake",
            model=model,
            reasoning_effort=reasoning_effort,
            read_only=True,
        )

    async def run(
        self,
        prompt: str,
        *,
        workspace: Path | None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> str | ProviderResponse:
        self.calls.append((prompt, workspace, model, reasoning_effort))
        result = self.handler(prompt, workspace, model)
        if inspect.isawaitable(result):
            return await result
        return result
