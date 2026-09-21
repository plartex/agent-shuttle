"""Evaluation orchestration."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable

from .batching import plan_batches
from .executors import BatchRequest, LlmCheckExecutor
from .models import CheckResult, EvaluationProfile, EvaluationReport, EvaluationTarget
from .providers.base import AgentProvider
from .scoring import summarize


class EvaluationService:
    def __init__(self, provider: AgentProvider):
        self.provider = provider
        self.executors = {"llm": LlmCheckExecutor(provider)}

    async def evaluate(
        self,
        target: EvaluationTarget,
        profile: EvaluationProfile,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        batch_size: int = 10,
        debug: bool = False,
        on_debug_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> EvaluationReport:
        batches = plan_batches(profile.rules, batch_size=batch_size)
        started = monotonic()
        trace: list[dict[str, Any]] = []

        def emit(event: str, details: dict[str, Any]) -> None:
            if not debug:
                return
            entry = {
                "time": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(monotonic() - started, 3),
                "event": event,
                **details,
            }
            trace.append(entry)
            if on_debug_event:
                on_debug_event(entry)

        collected: dict[str, CheckResult] = {}
        warnings: list[str] = []
        if not self.provider.capabilities.read_only:
            warnings.append("The selected provider cannot guarantee a read-only agent sandbox.")
        if target.kind != "snippet" and not self.provider.capabilities.file_targets:
            raise ValueError("The selected provider does not support file or project targets")
        if target.kind == "project" and not self.provider.capabilities.dynamic_workspace:
            raise ValueError(
                "Project evaluation is not supported by this provider yet: the bridge has a "
                "fixed workspace and does not send project content to the agent. "
                "No model calls were made. Use file or snippet targets."
            )

        emit("run_started", {
            "target_kind": target.kind,
            "provider": self.provider.descriptor(model, reasoning_effort).to_dict(),
            "rules": len(profile.rules),
            "batches": len(batches),
            "batch_size": batch_size,
        })

        for number, rules in enumerate(batches, start=1):
            executor_ids = {rule.executor for rule in rules}
            if len(executor_ids) != 1:
                raise ValueError("A batch cannot mix check executors")
            executor_id = next(iter(executor_ids))
            executor = self.executors.get(executor_id)
            if executor is None:
                raise ValueError(f"Unknown check executor: {executor_id}")
            batch_id = f"batch-{number:04d}"
            batch_started = monotonic()
            emit("batch_started", {
                "batch_id": batch_id,
                "scope": rules[0].scope,
                "rule_ids": [rule.id for rule in rules],
            })
            try:
                batch_results = await executor.execute(
                    BatchRequest(
                        batch_id=batch_id,
                        target=target,
                        rules=rules,
                        model=model,
                        reasoning_effort=reasoning_effort,
                        emit=emit if debug else None,
                    )
                )
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                emit("batch_error", {
                    "batch_id": batch_id,
                    "error_type": type(exc).__name__,
                    "message": str(exc).splitlines()[0][:300],
                })
                warnings.append(f"{batch_id} failed: {reason}")
                batch_results = tuple(
                    CheckResult(
                        rule_id=rule.id,
                        status="error",
                        severity=rule.severity,
                        reason=reason,
                    )
                    for rule in rules
                )
            for result in batch_results:
                if result.rule_id in collected:
                    raise RuntimeError(f"Duplicate aggregated result: {result.rule_id}")
                collected[result.rule_id] = result
            emit("batch_finished", {
                "batch_id": batch_id,
                "duration_seconds": round(monotonic() - batch_started, 3),
                "statuses": dict(Counter(item.status for item in batch_results)),
            })

        ordered = tuple(collected[rule.id] for rule in profile.rules)
        usage_totals: Counter[str] = Counter()
        for entry in trace:
            if entry["event"] == "agent_response" and isinstance(entry.get("usage"), dict):
                usage_totals.update(entry["usage"])
        agent_requests = sum(entry["event"] == "agent_request" for entry in trace)
        usage_reported_calls = sum(
            entry["event"] == "agent_response" and bool(entry.get("usage"))
            for entry in trace
        )
        emit("run_finished", {
            "duration_seconds": round(monotonic() - started, 3),
            "statuses": dict(Counter(item.status for item in ordered)),
            "usage_totals": dict(usage_totals) if usage_totals else None,
            "agent_requests": agent_requests,
            "usage_reported_calls": usage_reported_calls,
        })
        return EvaluationReport(
            run_id=str(uuid.uuid4()),
            profile=profile,
            target=target,
            provider=self.provider.descriptor(model, reasoning_effort),
            summary=summarize(ordered),
            results=ordered,
            warnings=tuple(warnings),
            debug_trace=tuple(trace),
        )
