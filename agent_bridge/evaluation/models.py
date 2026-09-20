"""Domain models for rule-based evaluations."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


TargetKind = Literal["snippet", "file", "project"]
CheckStatus = Literal["passed", "failed", "skipped", "inconclusive", "error"]
Severity = Literal["low", "medium", "high", "critical"]

VALID_STATUSES = frozenset({"passed", "failed", "skipped", "inconclusive", "error"})
VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})


@dataclass(frozen=True)
class EvaluationTarget:
    kind: TargetKind
    content: str | None = None
    path: Path | None = None
    language: str | None = None

    @classmethod
    def snippet(cls, content: str, language: str | None = None) -> "EvaluationTarget":
        if not isinstance(content, str) or not content.strip():
            raise ValueError("snippet must contain code")
        language = language.strip() if isinstance(language, str) and language.strip() else None
        return cls(kind="snippet", content=content, language=language)

    @classmethod
    def file(cls, path: str | Path) -> "EvaluationTarget":
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise ValueError(f"file target is not a file: {resolved}")
        return cls(kind="file", path=resolved)

    @classmethod
    def project(cls, path: str | Path) -> "EvaluationTarget":
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(f"project target is not a directory: {resolved}")
        _reject_escaping_symlinks(resolved)
        return cls(kind="project", path=resolved)

    @property
    def workspace(self) -> Path | None:
        if self.kind == "file":
            assert self.path is not None
            return self.path.parent
        if self.kind == "project":
            return self.path
        return None

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "snippet":
            assert self.content is not None
            result: dict[str, Any] = {
                "kind": "snippet",
                "content_sha256": hashlib.sha256(self.content.encode("utf-8")).hexdigest(),
            }
            if self.language:
                result["language"] = self.language
            return result
        assert self.path is not None
        return {"kind": self.kind, "path": str(self.path)}


@dataclass(frozen=True)
class RuleDefinition:
    id: str
    title: str
    title_en: str
    severity: Severity
    scope: str
    category_id: str
    summary: str
    definition: str
    symptoms: tuple[str, ...]
    detection_hints: dict[str, str]
    tags: tuple[str, ...] = ()
    executor: str = "llm"

    def prompt_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "title_en": self.title_en,
            "severity": self.severity,
            "scope": self.scope,
            "summary": self.summary,
            "definition": self.definition,
            "symptoms": list(self.symptoms),
            "detection_hints": self.detection_hints,
        }


@dataclass(frozen=True)
class EvaluationProfile:
    id: str
    title: str
    version: str
    catalog_sha256: str
    rules: tuple[RuleDefinition, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "version": self.version,
            "catalog_sha256": self.catalog_sha256,
        }


@dataclass(frozen=True)
class Evidence:
    path: str | None
    start_line: int
    end_line: int
    excerpt: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "excerpt": self.excerpt,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CheckResult:
    rule_id: str
    status: CheckStatus
    severity: Severity
    confidence: float | None = None
    evidence: tuple[Evidence, ...] = ()
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "rule_id": self.rule_id,
            "status": self.status,
            "severity": self.severity,
            "confidence": self.confidence,
            "evidence": [item.to_dict() for item in self.evidence],
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result


@dataclass(frozen=True)
class EvaluationSummary:
    total: int
    passed: int
    failed: int
    skipped: int
    inconclusive: int
    errors: int
    findings: int
    quality_score: float | None
    assessment_coverage: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "inconclusive": self.inconclusive,
            "errors": self.errors,
            "findings": self.findings,
            "quality_score": self.quality_score,
            "assessment_coverage": self.assessment_coverage,
        }


@dataclass(frozen=True)
class ProviderDescriptor:
    id: str
    agent: str | None = None
    model: str | None = None
    read_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "model": self.model,
            "read_only": self.read_only,
        }


@dataclass(frozen=True)
class EvaluationReport:
    run_id: str
    profile: EvaluationProfile
    target: EvaluationTarget
    provider: ProviderDescriptor
    summary: EvaluationSummary
    results: tuple[CheckResult, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "profile": self.profile.to_dict(),
            "target": self.target.to_dict(),
            "provider": self.provider.to_dict(),
            "summary": self.summary.to_dict(),
            "warnings": list(self.warnings),
            "results": [item.to_dict() for item in self.results],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def _reject_escaping_symlinks(project: Path) -> None:
    for root, directories, files in os.walk(project, followlinks=False):
        for name in [*directories, *files]:
            candidate = Path(root) / name
            if not candidate.is_symlink():
                continue
            try:
                destination = candidate.resolve(strict=True)
                destination.relative_to(project)
            except (OSError, ValueError) as exc:
                raise ValueError(f"project contains a symlink outside its boundary: {candidate}") from exc
