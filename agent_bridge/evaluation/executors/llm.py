"""LLM-backed rule executor."""

from __future__ import annotations

import asyncio
from time import monotonic

from ..prompting import build_evaluation_prompt, build_repair_prompt
from ..providers.base import AgentProvider, ProviderResponse
from ..validation import BatchProtocolError, validate_batch_response
from .base import BatchRequest


class LlmCheckExecutor:
    id = "llm"

    def __init__(self, provider: AgentProvider):
        self.provider = provider

    async def execute(self, request: BatchRequest):
        prompt = build_evaluation_prompt(request.batch_id, request.target, request.rules)
        async def run(text: str, attempt: int) -> str:
            if request.emit:
                request.emit("agent_request", {
                    "batch_id": request.batch_id,
                    "attempt": attempt,
                    "prompt_bytes": len(text.encode("utf-8")),
                })
            call = self.provider.run(
                text,
                workspace=request.target.workspace,
                model=request.model,
                reasoning_effort=request.reasoning_effort,
            )
            if request.emit:
                started = monotonic()
                task = asyncio.create_task(call)
                try:
                    while True:
                        try:
                            response = await asyncio.wait_for(asyncio.shield(task), timeout=10)
                            break
                        except asyncio.TimeoutError:
                            request.emit("agent_waiting", {
                                "batch_id": request.batch_id,
                                "attempt": attempt,
                                "waiting_seconds": round(monotonic() - started, 1),
                            })
                except BaseException:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    raise
            else:
                response = await call
            raw = response.text if isinstance(response, ProviderResponse) else response
            if request.emit:
                request.emit("agent_response", {
                    "batch_id": request.batch_id,
                    "attempt": attempt,
                    "response_bytes": len(raw.encode("utf-8")),
                    "usage": response.usage if isinstance(response, ProviderResponse) else None,
                })
            return raw

        raw = await run(prompt, 1)
        try:
            results = validate_batch_response(
                raw,
                batch_id=request.batch_id,
                rules=request.rules,
                target=request.target,
            )
            if request.emit:
                request.emit("validated", {"batch_id": request.batch_id, "attempt": 1})
            return results
        except BatchProtocolError as first_error:
            if request.emit:
                request.emit("validation_failed", {
                    "batch_id": request.batch_id,
                    "attempt": 1,
                    "reason": str(first_error)[:500],
                })
            repair = build_repair_prompt(
                prompt,
                request.batch_id,
                tuple(rule.id for rule in request.rules),
                raw,
                str(first_error),
            )
            repaired = await run(repair, 2)
            try:
                results = validate_batch_response(
                    repaired,
                    batch_id=request.batch_id,
                    rules=request.rules,
                    target=request.target,
                )
                if request.emit:
                    request.emit("validated", {"batch_id": request.batch_id, "attempt": 2})
                return results
            except BatchProtocolError as second_error:
                raise BatchProtocolError(
                    f"invalid response after one repair: {second_error}; first error: {first_error}"
                ) from second_error
