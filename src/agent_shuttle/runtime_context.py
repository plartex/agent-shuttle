"""Process role inherited by an Agent Shuttle instance inside a worker."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


WORKER_CONTEXT_ENV = "AGENT_SHUTTLE_PARENT_CONTEXT"
WORKER_CONTEXT_VALUE = "worker"
NESTED_DISPATCH_ERROR = (
    "nested_dispatch_disabled: this Agent Shuttle instance runs inside a worker"
)


def is_worker_context(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(WORKER_CONTEXT_ENV) == WORKER_CONTEXT_VALUE


def worker_env(env: Mapping[str, str]) -> dict[str, str]:
    """Mark a real worker after all user supplied environment changes."""
    return {**env, WORKER_CONTEXT_ENV: WORKER_CONTEXT_VALUE}


def require_coordinator(worker: bool | None = None) -> None:
    nested = is_worker_context() if worker is None else worker
    if nested:
        raise RuntimeError(NESTED_DISPATCH_ERROR)


def nested_data_path(path: Path | str) -> Path:
    """Keep a nested instance's SQLite and ticket files away from its parent."""
    resolved = Path(path).resolve()
    return resolved if resolved.parent.name == "nested" else resolved.parent / "nested" / resolved.name
