"""Execution, cancellation and native-session lifetime for library tasks."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
from pathlib import Path
from time import monotonic, time
from typing import Any

from .backends import BackendResponse
from .event_stream import EventStreamService
from .library_repository import LibraryTaskRepository


_FINAL = frozenset({"completed", "failed", "canceled", "rejected"})


class _NativeSession:
    def __init__(self, session):
        self.session = session
        self.lock = asyncio.Lock()


class WorkerLifecycleService:
    def __init__(self, repository: LibraryTaskRepository, events: EventStreamService,
                 backends: dict[str, Any], workspace: Path, database: Path | None,
                 execution_timeout_seconds: float, stall_timeout_seconds: float):
        self.repository = repository
        self.events = events
        self.backends = backends
        self.workspace = workspace
        self.database = database
        self.execution_timeout_seconds = execution_timeout_seconds
        self.stall_timeout_seconds = stall_timeout_seconds
        self._active: dict[str, asyncio.Task] = {}
        self._native_sessions: dict[str, _NativeSession] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}

    def _resumable_agents(self) -> set[str]:
        return {name for name, backend in self.backends.items()
                if callable(getattr(backend, "resume_session", None))
                and (getattr(backend, "can_attempt_resume_after_restart", False)
                     or getattr(backend, "supports_resume_after_restart", True))}

    def recover(self) -> None:
        self.repository.recover_interrupted(self._resumable_agents())

    async def shutdown(self) -> None:
        active = list(self._active.values())
        for running in active:
            running.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        for row in self.repository.list_tasks():
            if row["state"] in {"submitted", "working"}:
                self.repository.update_task(row["id"], "canceled", {"reason": "manager closed"})
        for native in list(self._native_sessions.values()):
            await native.session.close()
        self._native_sessions.clear()
        self.repository.suspend_open_sessions(self._resumable_agents())

    def launch(self, task_id: str) -> None:
        running = asyncio.create_task(self._execute(task_id))
        self._active[task_id] = running
        running.add_done_callback(lambda _: (self._active.pop(task_id, None),
                                             self.events.detach(task_id)))

    async def cancel(self, task_id: str):
        row = self.repository.task(task_id)
        if row["state"] in _FINAL:
            return
        running = self._active.get(task_id)
        if running is not None:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        if self.repository.task(task_id)["state"] not in _FINAL:
            self.repository.update_task(task_id, "canceled", {"reason": "explicit cancellation"})
        session_id = self.repository.task(task_id)["session_id"]
        if session_id is not None:
            await self.interrupt_session(session_id)

    async def _execute(self, task_id: str):
        row = self.repository.task(task_id)
        self.repository.update_task(task_id, "working")
        deadline = monotonic() + self.execution_timeout_seconds
        exclusive = sum(not running.done() for running in self._active.values()) <= 1
        running = None
        backend_started = False
        native_started = False
        last_activity = None

        async def on_event(event):
            nonlocal last_activity
            last_activity = monotonic()
            await self.events.publish_backend_event(task_id, event)

        async def invoke():
            nonlocal backend_started, native_started, last_activity
            backend = self.backends[row["agent_id"]]
            kwargs = {"reasoning_effort": row["reasoning_effort"],
                      "read_only": row["tool_policy"] == "read_only"}
            target = backend.open_session if row["session_id"] else backend.run
            if row["tool_policy"] is not None and "tool_policy" in inspect.signature(target).parameters:
                kwargs["tool_policy"] = row["tool_policy"]
            if row["output_schema"] is not None:
                kwargs["output_schema"] = json.loads(row["output_schema"])
            if row["session_id"] and "on_event" in inspect.signature(target).parameters:
                kwargs["on_event"] = on_event
            if row["session_id"]:
                session_id = row["session_id"]
                lock = self._session_locks.setdefault(session_id, asyncio.Lock())
                async with lock:
                    session_row = self.repository.session(session_id)
                    if session_row["state"] not in {"open", "suspended"}:
                        raise RuntimeError("Session was closed after cancellation; start a new session")
                    backend_started = native_started = True
                    last_activity = monotonic()
                    native = self._native_sessions.get(session_id)
                    if native is None:
                        if session_row["state"] == "suspended":
                            opened = await backend.resume_session(session_row["native_id"], row["model"], **kwargs)
                        else:
                            opened = await backend.open_session(row["model"], **kwargs)
                        native = _NativeSession(opened)
                        self._native_sessions[session_id] = native
                        self.repository.set_session_state(
                            session_id, "open", getattr(opened, "native_id", None),
                            resume_supported=getattr(opened, "supports_resume", True),
                        )
                    if "on_event" in inspect.signature(native.session.ask).parameters:
                        return await native.session.ask(row["prompt"], on_event=on_event)
                    return await native.session.ask(row["prompt"])
            if "on_event" in inspect.signature(backend.run).parameters:
                kwargs["on_event"] = on_event
            backend_started = True
            last_activity = monotonic()
            return await backend.run(row["prompt"], row["model"], **kwargs)

        try:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Worker exceeded its execution budget")
            try:
                before_files = (await asyncio.wait_for(
                    self._workspace_changes(min(5, remaining)), remaining)
                    if exclusive else None)
            except asyncio.TimeoutError as exc:
                raise TimeoutError("Worker exceeded its execution budget") from exc
            if deadline - monotonic() <= 0:
                raise TimeoutError("Worker exceeded its execution budget")
            running = asyncio.create_task(invoke())
            while not running.done():
                now = monotonic()
                remaining = deadline - now
                if backend_started:
                    inactivity_remaining = self.stall_timeout_seconds - (now - last_activity)
                    remaining = min(remaining, inactivity_remaining)
                if remaining <= 0:
                    if now >= deadline:
                        raise TimeoutError("Worker exceeded its execution budget")
                    raise TimeoutError("Worker exceeded its inactivity budget")
                await asyncio.wait({running}, timeout=min(remaining, 0.05) if not backend_started else remaining)
            answer = await running
            if monotonic() >= deadline:
                raise TimeoutError("Worker exceeded its execution budget")
            if isinstance(answer, BackendResponse):
                text, usage, details = answer.text, answer.usage, answer.details
            else:
                text, usage, details = str(answer), None, None
            observed_model = details.get("observed_model") if isinstance(details, dict) else None
            if not isinstance(observed_model, str) or not observed_model:
                observed_model = None
            warning_data = details.get("warnings") if isinstance(details, dict) else None
            warnings = tuple(item for item in warning_data if isinstance(item, str)) if isinstance(warning_data, (list, tuple)) else ()
            exclusive = exclusive and sum(not task.done() for task in self._active.values()) <= 1
            after_files = None
            remaining = deadline - monotonic()
            if exclusive and before_files == set() and remaining > 0:
                try:
                    after_files = await asyncio.wait_for(
                        self._workspace_changes(min(5, remaining)), remaining)
                except asyncio.TimeoutError:
                    pass
            collected = after_files is not None
            changed = tuple(sorted(after_files)[:200]) if collected else ()
            self.repository.update_task(task_id, "completed", {"text": text}, text=text,
                                        usage=usage, details=details,
                                        observed_model=observed_model, warnings=warnings,
                                        files_changed=changed if collected else None,
                                        files_changed_state="collected" if collected else "unavailable")
        except asyncio.CancelledError:
            if running is not None:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            if row["session_id"] and native_started:
                await self.interrupt_session(row["session_id"])
            self.repository.update_task(task_id, "canceled", {"reason": "cancelled"})
        except Exception as exc:
            if running is not None:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            reusable = getattr(exc, "session_reusable", False)
            if row["session_id"] and native_started and not reusable:
                await self.interrupt_session(row["session_id"])
            code = ("worker_stalled" if isinstance(exc, TimeoutError) and "inactivity" in str(exc)
                    else "worker_timeout" if isinstance(exc, TimeoutError) else "backend_error")
            error = {"code": code, "type": type(exc).__name__, "message": str(exc),
                     "retryable": bool(reusable)}
            self.repository.update_task(task_id, "failed", error, error=error,
                                        usage=getattr(exc, "usage", None),
                                        details=getattr(exc, "details", None))
        finally:
            if row["session_id"]:
                self.repository.touch_session(row["session_id"])

    async def _workspace_changes(self, timeout_seconds: float = 5) -> set[str] | None:
        if not any((directory / ".git").exists() for directory in (self.workspace, *self.workspace.parents)):
            return None
        process = None
        try:
            async with asyncio.timeout(timeout_seconds):
                process = await asyncio.create_subprocess_exec(
                    "git", "-C", str(self.workspace), "-c", "core.quotePath=false",
                    "status", "--porcelain=v1", "-z", "--untracked-files=all", "--",
                    ".", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
                output, _ = await process.communicate()
        except asyncio.TimeoutError:
            return None
        except OSError:
            return None
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if process.returncode != 0:
            return None
        chunks = output.split(b"\0")
        paths = set()
        database_relative = None
        if self.database is not None:
            try:
                database_relative = self.database.relative_to(self.workspace).as_posix()
            except ValueError:
                pass
        index = 0
        while index < len(chunks) and chunks[index]:
            item = chunks[index]
            if len(item) < 4:
                return None
            path = item[3:].decode("utf-8", errors="replace").replace("\\", "/")
            if database_relative is None or not (path == database_relative or path.startswith(database_relative + ".")
                                                 or path.startswith(database_relative + "-")):
                paths.add(path)
            if b"R" in item[:2] or b"C" in item[:2]:
                index += 1
            index += 1
        return paths

    async def interrupt_session(self, session_id: str):
        self.repository.set_session_state(session_id, "interrupted")
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            native = self._native_sessions.pop(session_id, None)
            if native is not None:
                await native.session.close()

    async def reap_idle_sessions(self, idle_seconds: float = 1800) -> list[str]:
        if isinstance(idle_seconds, bool) or not isinstance(idle_seconds, (int, float)) or not math.isfinite(idle_seconds) or idle_seconds <= 0:
            raise ValueError("idle_seconds must be positive finite")
        expired = []
        for session_id in self.repository.idle_sessions(time() - idle_seconds):
            if any(not running.done() and self.repository.task(task_id)["session_id"] == session_id
                   for task_id, running in list(self._active.items())):
                continue
            await self.end_session(session_id)
            expired.append(session_id)
        return expired

    async def end_session(self, session_id: str) -> bool:
        row = self.repository.session(session_id)
        if row["state"] == "ended":
            return False
        active = [task_id for task_id in self._active
                  if self.repository.task(task_id)["session_id"] == session_id]
        for task_id in active:
            await self.cancel(task_id)
        native = self._native_sessions.pop(session_id, None)
        if native is not None:
            async with native.lock:
                await native.session.close()
        self.repository.set_session_state(session_id, "ended")
        return True
