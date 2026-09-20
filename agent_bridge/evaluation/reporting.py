"""Human-readable evaluation reports."""

from __future__ import annotations

from .models import EvaluationReport


def format_text_report(report: EvaluationReport) -> str:
    summary = report.summary
    quality = "N/A" if summary.quality_score is None else f"{summary.quality_score:.1f}%"
    lines = [
        f"{report.profile.title}: {summary.passed}/{summary.total} PASSED",
        (
            f"FAILED: {summary.failed} · SKIPPED: {summary.skipped} · "
            f"INCONCLUSIVE: {summary.inconclusive} · ERRORS: {summary.errors}"
        ),
        f"Code quality: {quality} · Assessment coverage: {summary.assessment_coverage:.1f}%",
    ]
    failed = [result for result in report.results if result.status == "failed"]
    if failed:
        titles = {rule.id: rule.title for rule in report.profile.rules}
        lines.append("")
        lines.append("Findings:")
        for result in failed:
            title = titles.get(result.rule_id, result.rule_id)
            lines.append(f"- [{result.severity.upper()}] {result.rule_id}: {title}")
            for evidence in result.evidence:
                location = "snippet" if evidence.path is None else evidence.path
                lines.append(
                    f"  {location}:{evidence.start_line}-{evidence.end_line} — {evidence.reason}"
                )
    unresolved = [result for result in report.results if result.status in {"skipped", "inconclusive", "error"}]
    if unresolved:
        lines.append("")
        lines.append("Unresolved checks:")
        for result in unresolved:
            lines.append(f"- [{result.status.upper()}] {result.rule_id}: {result.reason or 'No reason provided'}")
    if report.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in report.warnings)
    return "\n".join(lines)
