"""Small programmable provider for tests and embedding examples."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Awaitable, Callable

from ..models import ProviderDescriptor
from .base import ProviderCapabilities


FakeHandler = Callable[[str, Path | None, str | None], str | Awaitable[str]]


class FakeAgentProvider:
    capabilities = ProviderCapabilities(
        file_targets=True,
        dynamic_workspace=True,
        read_only=True,
        structured_output=True,
    )

    def __init__(self, handler: FakeHandler):
        self.handler = handler
        self.calls: list[tuple[str, Path | None, str | None]] = []

    def descriptor(self, model: str | None = None) -> ProviderDescriptor:
        return ProviderDescriptor(id="fake", agent="fake", model=model, read_only=True)

    async def run(
        self,
        prompt: str,
        *,
        workspace: Path | None,
        model: str | None = None,
    ) -> str:
        self.calls.append((prompt, workspace, model))
        result = self.handler(prompt, workspace, model)
        if inspect.isawaitable(result):
            return await result
        return result
