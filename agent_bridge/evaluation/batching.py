"""Stable rule batching."""

from __future__ import annotations

from collections import OrderedDict

from .models import RuleDefinition


def plan_batches(rules: tuple[RuleDefinition, ...], batch_size: int = 10) -> tuple[tuple[RuleDefinition, ...], ...]:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    grouped: OrderedDict[str, list[RuleDefinition]] = OrderedDict()
    for rule in rules:
        grouped.setdefault(rule.scope, []).append(rule)
    batches: list[tuple[RuleDefinition, ...]] = []
    for group in grouped.values():
        for offset in range(0, len(group), batch_size):
            batches.append(tuple(group[offset : offset + batch_size]))
    return tuple(batches)
