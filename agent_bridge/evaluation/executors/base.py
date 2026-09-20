"""Common check-executor contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models import CheckResult, EvaluationTarget, RuleDefinition


@dataclass(frozen=True)
class BatchRequest:
    batch_id: str
    target: EvaluationTarget
    rules: tuple[RuleDefinition, ...]
    model: str | None = None
    reasoning_effort: str | None = None


class CheckExecutor(Protocol):
    id: str

    async def execute(self, request: BatchRequest) -> tuple[CheckResult, ...]: ...
