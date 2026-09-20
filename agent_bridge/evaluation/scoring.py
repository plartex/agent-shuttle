"""Deterministic evaluation aggregation and scoring."""

from __future__ import annotations

from collections import Counter

from .models import CheckResult, EvaluationSummary


SEVERITY_WEIGHTS = {"low": 1, "medium": 2, "high": 3, "critical": 5}


def summarize(results: tuple[CheckResult, ...]) -> EvaluationSummary:
    counts = Counter(result.status for result in results)
    passed_weight = sum(SEVERITY_WEIGHTS[result.severity] for result in results if result.status == "passed")
    failed_weight = sum(SEVERITY_WEIGHTS[result.severity] for result in results if result.status == "failed")
    assessed_weight = passed_weight + failed_weight
    quality = 100.0 * passed_weight / assessed_weight if assessed_weight else None
    assessed_count = counts["passed"] + counts["failed"]
    coverage = 100.0 * assessed_count / len(results) if results else 0.0
    return EvaluationSummary(
        total=len(results),
        passed=counts["passed"],
        failed=counts["failed"],
        skipped=counts["skipped"],
        inconclusive=counts["inconclusive"],
        errors=counts["error"],
        findings=sum(len(result.evidence) for result in results if result.status == "failed"),
        quality_score=quality,
        assessment_coverage=coverage,
    )
