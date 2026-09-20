"""Evaluation orchestration."""

from __future__ import annotations

import uuid

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
        batch_size: int = 10,
    ) -> EvaluationReport:
        batches = plan_batches(profile.rules, batch_size=batch_size)
        collected: dict[str, CheckResult] = {}
        warnings: list[str] = []
        if not self.provider.capabilities.read_only:
            warnings.append("The selected provider cannot guarantee a read-only agent sandbox.")
        if target.kind != "snippet" and not self.provider.capabilities.file_targets:
            raise ValueError("The selected provider does not support file or project targets")

        for number, rules in enumerate(batches, start=1):
            executor_ids = {rule.executor for rule in rules}
            if len(executor_ids) != 1:
                raise ValueError("A batch cannot mix check executors")
            executor_id = next(iter(executor_ids))
            executor = self.executors.get(executor_id)
            if executor is None:
                raise ValueError(f"Unknown check executor: {executor_id}")
            batch_id = f"batch-{number:04d}"
            try:
                batch_results = await executor.execute(
                    BatchRequest(batch_id=batch_id, target=target, rules=rules, model=model)
                )
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
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

        ordered = tuple(collected[rule.id] for rule in profile.rules)
        return EvaluationReport(
            run_id=str(uuid.uuid4()),
            profile=profile,
            target=target,
            provider=self.provider.descriptor(model),
            summary=summarize(ordered),
            results=ordered,
            warnings=tuple(warnings),
        )
