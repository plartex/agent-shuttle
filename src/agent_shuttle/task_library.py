"""Public Python facade for transport-independent tasks and sessions."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, time
from typing import Any

from .event_stream import EventStreamService
from .library_repository import LibraryTaskRepository
from .profiles import ToolPolicy
from .runtime_context import is_worker_context, nested_data_path, require_coordinator
from .structured import encode_output_schema
from .worker_lifecycle import WorkerLifecycleService

_FINAL = frozenset({"completed", "failed", "canceled", "rejected"})


@dataclass(frozen=True)
class TaskStatus:
    id: str
    agent_id: str
    session_id: str | None
    state: str
    created_at: float
    updated_at: float
    silent_for_seconds: float | None
    error: dict | None = None


@dataclass(frozen=True)
class TaskResult(TaskStatus):
    text: str = ""
    usage: dict | None = None
    details: dict | None = None
    requested_model: str | None = None
    observed_model: str | None = None
    warnings: tuple[str, ...] = ()
    files_changed: tuple[str, ...] = ()
    files_changed_state: str = "pending"


@dataclass(frozen=True)
class SessionInfo:
    id: str
    agent_id: str
    model: str | None
    reasoning_effort: str | None
    tool_policy: str | None
    state: str
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class AgentInfo:
    id: str
    backend: str
    supports_sessions: bool
    supports_resume_after_restart: bool
    tool_policies: tuple[str, ...]


@dataclass(frozen=True)
class Task:
    manager: "TaskManager"
    id: str

    async def status(self) -> TaskStatus:
        return await self.manager.status(self.id)

    async def wait(self, timeout: float | None = None) -> TaskStatus:
        return await self.manager.wait(self.id, timeout)

    async def result(self) -> TaskResult:
        await self.wait()
        return await self.manager.result(self.id)

    async def cancel(self) -> TaskStatus:
        return await self.manager.cancel(self.id)

    async def result_page(self, cursor: int = 0, limit: int = 60000) -> dict:
        return await self.manager.result_page(self.id, cursor, limit)

    async def transcript(self, cursor: int = 0, limit: int = 100) -> dict:
        return await self.manager.transcript(self.id, cursor, limit)

    async def event_page(self, seq: int, cursor: int = 0, limit: int = 60000) -> dict:
        return await self.manager.event_page(self.id, seq, cursor, limit)

    async def events(self, cursor: int = 0):
        async for event in self.manager.event_stream.events(self.id, cursor):
            yield event


@dataclass(frozen=True)
class Session:
    manager: "TaskManager"
    id: str

    async def dispatch(self, prompt: str, *, request_id: str | None = None) -> Task:
        return await self.manager.dispatch_session(self.id, prompt, request_id=request_id)

    async def end(self) -> bool:
        return await self.manager.end_session(self.id)


class TaskManager:
    """Compose persistence, event delivery and worker execution for one workspace."""

    def __init__(self, backends: dict[str, Any], *, workspace: Path | str,
                 database: Path | str | None = None, memory: bool = False,
                 runtime_context: str | None = None,
                 info_providers: dict[str, Any] | None = None,
                 execution_timeout_seconds: float = 1800,
                 stall_timeout_seconds: float = 1800):
        for value in (execution_timeout_seconds, stall_timeout_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("Execution and stall budgets must be positive finite numbers")
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("workspace must be a directory")
        self.backends = dict(backends)
        self.info_providers = dict(info_providers or {})
        if runtime_context not in (None, "coordinator", "worker"):
            raise ValueError("runtime_context must be coordinator or worker")
        self.runtime_context = ("worker" if is_worker_context() or runtime_context == "worker"
                                else "coordinator")
        database_path = Path(database or self.workspace / ".agent-shuttle" / "library-tasks.sqlite3")
        self.database = (None if memory else
                         (nested_data_path(database_path) if self.runtime_context == "worker"
                          else database_path.resolve()))
        self.execution_timeout_seconds = execution_timeout_seconds
        self.stall_timeout_seconds = stall_timeout_seconds
        self.repository = LibraryTaskRepository(self.database)
        self.event_stream = EventStreamService(self.repository)
        self.workers = WorkerLifecycleService(
            self.repository, self.event_stream, self.backends, self.workspace, self.database,
            execution_timeout_seconds, stall_timeout_seconds)
        self._closed = False
        self._open = False

    @classmethod
    def for_workspace(cls, workspace: Path | str, *, antigravity_command: str = "agy",
                      database: Path | str | None = None) -> "TaskManager":
        from .backends import AntigravityCliBackend, CodexBackend
        from .info import AntigravityCliInfo, CodexInfo
        root = Path(workspace).resolve(strict=True)
        return cls({"codex": CodexBackend(root),
                    "antigravity": AntigravityCliBackend(root, antigravity_command)},
                   workspace=root, database=database,
                   info_providers={"codex": CodexInfo(root),
                                   "antigravity": AntigravityCliInfo(root, antigravity_command)})

    async def __aenter__(self) -> "TaskManager":
        if self._open or self._closed:
            raise RuntimeError("TaskManager is already open or closed")
        self.repository.open()
        try:
            self.workers.recover()
        except BaseException:
            self.repository.close()
            raise
        self._open = True
        return self

    async def __aexit__(self, *_):
        try:
            if self._open:
                await self.workers.shutdown()
        finally:
            self.repository.close()
            self._open = False
            self._closed = True

    @staticmethod
    def _status(row) -> TaskStatus:
        silent = None if row["state"] in _FINAL or row["last_activity_at"] is None else max(0, time() - row["last_activity_at"])
        return TaskStatus(row["id"], row["agent_id"], row["session_id"], row["state"],
                          row["created_at"], row["updated_at"], silent,
                          json.loads(row["error"]) if row["error"] else None)

    async def dispatch(self, agent_id: str, prompt: str, *, model: str | None = None,
                       reasoning_effort: str | None = None, tool_policy: str | None = None,
                       session_id: str | None = None, request_id: str | None = None,
                       task_id: str | None = None, event_sink=None,
                       output_schema: dict | None = None) -> Task:
        require_coordinator(self.runtime_context == "worker")
        self.repository.ensure_open()
        if agent_id not in self.backends:
            raise ValueError(f"Unknown agent {agent_id!r}")
        schema = self._schema(agent_id, output_schema, bool(session_id))
        if session_id is None:
            preference = await self.get_preference(agent_id)
            if model is None:
                model = preference["model"]
            if reasoning_effort is None:
                reasoning_effort = preference["reasoning_effort"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be nonempty")
        if tool_policy is not None and tool_policy not in {policy.value for policy in ToolPolicy}:
            raise ValueError("Unknown tool_policy")
        if request_id is not None:
            request_id = str(uuid.UUID(request_id))
        if session_id is not None:
            session_id = str(uuid.UUID(session_id))
            session = self.repository.session(session_id)
            if session["state"] not in {"open", "suspended"}:
                raise RuntimeError("Session was closed after cancellation; start a new session")
            if (session["agent_id"], session["model"], session["reasoning_effort"], session["tool_policy"]) != (agent_id, model, reasoning_effort, tool_policy):
                raise ValueError("Session agent, model, effort and tool policy are pinned")
            if session["output_schema"] != schema:
                raise ValueError("Session output schema is pinned")
        fingerprint = hashlib.sha256(json.dumps(
            [agent_id, prompt, model, reasoning_effort, tool_policy, session_id] + ([schema] if schema else []),
            ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()
        if request_id is not None:
            existing = self.repository.task_by_request(request_id)
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("request_id is already bound to another request")
                return Task(self, existing["id"])
        task_id = str(uuid.UUID(task_id)) if task_id is not None else str(uuid.uuid4())
        self.repository.create_task({
            "id": task_id, "agent_id": agent_id, "session_id": session_id,
            "request_id": request_id, "fingerprint": fingerprint, "prompt": prompt,
            "model": model, "reasoning_effort": reasoning_effort,
            "tool_policy": tool_policy, "output_schema": schema,
        }, {"prompt": prompt})
        if event_sink is not None:
            self.event_stream.attach(task_id, event_sink)
        self.workers.launch(task_id)
        return Task(self, task_id)

    async def list_agents(self) -> list[AgentInfo]:
        self.repository.ensure_open()
        result = []
        for agent_id, backend in sorted(self.backends.items()):
            name = type(backend).__name__
            if name == "CodexBackend":
                policies = ("read_only", "workspace_write", "full_access")
            elif name == "AntigravityCliBackend":
                policies = ("no_tools", "read_only", "workspace_write")
                if getattr(backend, "dangerously_skip_permissions", False):
                    policies += ("full_access",)
            elif name == "ProfiledBackend":
                ordered = tuple(policy.value for policy in ToolPolicy)
                maximum = backend.profile.max_tool_policy.value
                policies = (() if backend.profile.runtime == "acp" else
                            ordered[:ordered.index(maximum) + 1])
            else:
                policies = ()
            supports_sessions = name != "AntigravitySdkBackend" and callable(getattr(backend, "open_session", None))
            result.append(AgentInfo(agent_id, name, supports_sessions,
                                    callable(getattr(backend, "resume_session", None))
                                    and getattr(backend, "supports_resume_after_restart", True), policies))
        return result

    async def agent_info(self, agent_id: str, *, capabilities: bool = True,
                         usage: bool = True) -> dict:
        infos = {item.id: item for item in await self.list_agents()}
        if agent_id not in infos:
            raise ValueError(f"Unknown agent {agent_id!r}")
        from dataclasses import asdict
        result = asdict(infos[agent_id])
        provider = self.info_providers.get(agent_id)
        if provider is not None:
            result.update(await provider.fetch(capabilities=capabilities, usage=usage))
        return result

    async def get_preference(self, agent_id: str) -> dict:
        if agent_id not in self.backends:
            raise ValueError(f"Unknown agent {agent_id!r}")
        return self.repository.preference(agent_id)

    async def set_preference(self, agent_id: str, *, model: str | None = None,
                             reasoning_effort: str | None = None) -> dict:
        require_coordinator(self.runtime_context == "worker")
        if agent_id not in self.backends:
            raise ValueError(f"Unknown agent {agent_id!r}")
        for value in (model, reasoning_effort):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("Model and reasoning effort must be nonempty strings")
        return self.repository.set_preference(agent_id, model, reasoning_effort)

    async def get(self, task_id: str) -> Task:
        self.repository.task(task_id)
        return Task(self, task_id)

    async def list_tasks(self, *, session_id: str | None = None) -> list[TaskStatus]:
        return [self._status(row) for row in self.repository.list_tasks(session_id)]

    async def status(self, task_id: str) -> TaskStatus:
        return self._status(self.repository.task(task_id))

    async def wait(self, task_id: str, timeout: float | None = None) -> TaskStatus:
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be finite and nonnegative")
        deadline = None if timeout is None else monotonic() + timeout
        while True:
            status = await self.status(task_id)
            if status.state in _FINAL or (deadline is not None and monotonic() >= deadline):
                return status
            await asyncio.sleep(0.05 if deadline is None else min(0.05, max(0, deadline - monotonic())))

    async def result(self, task_id: str) -> TaskResult:
        row = self.repository.task(task_id)
        status = self._status(row)
        return TaskResult(**status.__dict__, text=row["text"],
                          usage=json.loads(row["usage"]) if row["usage"] else None,
                          details=json.loads(row["details"]) if row["details"] else None,
                          requested_model=row["model"], observed_model=row["observed_model"],
                          warnings=tuple(json.loads(row["warnings"])) if row["warnings"] else (),
                          files_changed=tuple(json.loads(row["files_changed"])) if row["files_changed"] else (),
                          files_changed_state=row["files_changed_state"])

    async def cancel(self, task_id: str) -> TaskStatus:
        require_coordinator(self.runtime_context == "worker")
        await self.workers.cancel(task_id)
        return await self.status(task_id)

    async def result_page(self, task_id: str, cursor: int = 0, limit: int = 60000) -> dict:
        self.event_stream._page(cursor, limit, 60000)
        row = self.repository.task(task_id)
        end = min(cursor + limit, len(row["text"]))
        return {"task_id": task_id, "state": row["state"], "text": row["text"][cursor:end],
                "next_cursor": end if end < len(row["text"]) else None,
                "total_size": len(row["text"])}

    async def transcript(self, task_id: str, cursor: int = 0, limit: int = 100) -> dict:
        return self.event_stream.transcript(task_id, cursor, limit)

    async def event_page(self, task_id: str, seq: int, cursor: int = 0,
                         limit: int = 60000) -> dict:
        return self.event_stream.event_page(task_id, seq, cursor, limit)

    @staticmethod
    def _session_info(row) -> SessionInfo:
        return SessionInfo(row["id"], row["agent_id"], row["model"], row["reasoning_effort"],
                           row["tool_policy"], row["state"], row["created_at"], row["updated_at"])

    async def create_session(self, agent_id: str, *, model: str | None = None,
                             reasoning_effort: str | None = None,
                             tool_policy: str | None = None,
                             output_schema: dict | None = None) -> Session:
        require_coordinator(self.runtime_context == "worker")
        self.repository.ensure_open()
        if agent_id not in self.backends:
            raise ValueError(f"Unknown agent {agent_id!r}")
        schema = self._schema(agent_id, output_schema, True)
        preference = await self.get_preference(agent_id)
        if model is None:
            model = preference["model"]
        if reasoning_effort is None:
            reasoning_effort = preference["reasoning_effort"]
        if tool_policy is not None and tool_policy not in {policy.value for policy in ToolPolicy}:
            raise ValueError("Unknown tool_policy")
        session_id = str(uuid.uuid4())
        self.repository.create_session(session_id, agent_id, model, reasoning_effort, tool_policy, schema)
        return Session(self, session_id)

    async def ensure_session(self, session_id: str, agent_id: str, *, model: str | None = None,
                             reasoning_effort: str | None = None,
                             tool_policy: str | None = None,
                             output_schema: dict | None = None) -> Session:
        require_coordinator(self.runtime_context == "worker")
        session_id = str(uuid.UUID(session_id))
        schema = self._schema(agent_id, output_schema, True)
        row = self.repository.find_session(session_id)
        if row is None:
            self.repository.create_session(session_id, agent_id, model, reasoning_effort, tool_policy, schema)
        elif (row["agent_id"], row["model"], row["reasoning_effort"], row["tool_policy"]) != (agent_id, model, reasoning_effort, tool_policy):
            raise ValueError("Session agent, model, effort and tool policy are pinned")
        elif row["output_schema"] != schema:
            raise ValueError("Session output schema is pinned")
        return Session(self, session_id)

    async def dispatch_session(self, session_id: str, prompt: str, *, request_id: str | None = None) -> Task:
        require_coordinator(self.runtime_context == "worker")
        row = self.repository.session(session_id)
        return await self.dispatch(row["agent_id"], prompt, model=row["model"],
                                   reasoning_effort=row["reasoning_effort"],
                                   tool_policy=row["tool_policy"], session_id=session_id,
                                   request_id=request_id,
                                   output_schema=json.loads(row["output_schema"]) if row["output_schema"] else None)

    def _schema(self, agent_id, output_schema, session):
        schema = encode_output_schema(output_schema)
        if schema is not None:
            backend = self.backends.get(agent_id)
            target = getattr(backend, "open_session" if session else "run", None)
            if target is None or "output_schema" not in inspect.signature(target).parameters:
                raise ValueError(f"Agent {agent_id!r} does not support native structured output")
        return schema

    async def list_sessions(self) -> list[SessionInfo]:
        return [self._session_info(row) for row in self.repository.list_sessions()]

    async def reap_idle_sessions(self, idle_seconds: float = 1800) -> list[str]:
        return await self.workers.reap_idle_sessions(idle_seconds)

    async def session(self, session_id: str) -> Session:
        self.repository.session(session_id)
        return Session(self, session_id)

    async def end_session(self, session_id: str) -> bool:
        require_coordinator(self.runtime_context == "worker")
        return await self.workers.end_session(session_id)
