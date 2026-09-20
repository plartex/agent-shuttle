"""LLM-backed rule executor."""

from __future__ import annotations

from ..prompting import build_evaluation_prompt, build_repair_prompt
from ..providers.base import AgentProvider
from ..validation import BatchProtocolError, validate_batch_response
from .base import BatchRequest


class LlmCheckExecutor:
    id = "llm"

    def __init__(self, provider: AgentProvider):
        self.provider = provider

    async def execute(self, request: BatchRequest):
        prompt = build_evaluation_prompt(request.batch_id, request.target, request.rules)
        raw = await self.provider.run(
            prompt,
            workspace=request.target.workspace,
            model=request.model,
            reasoning_effort=request.reasoning_effort,
        )
        try:
            return validate_batch_response(
                raw,
                batch_id=request.batch_id,
                rules=request.rules,
                target=request.target,
            )
        except BatchProtocolError as first_error:
            repair = build_repair_prompt(
                prompt,
                request.batch_id,
                tuple(rule.id for rule in request.rules),
                raw,
                str(first_error),
            )
            repaired = await self.provider.run(
                repair,
                workspace=request.target.workspace,
                model=request.model,
                reasoning_effort=request.reasoning_effort,
            )
            try:
                return validate_batch_response(
                    repaired,
                    batch_id=request.batch_id,
                    rules=request.rules,
                    target=request.target,
                )
            except BatchProtocolError as second_error:
                raise BatchProtocolError(
                    f"invalid response after one repair: {second_error}; first error: {first_error}"
                ) from second_error
