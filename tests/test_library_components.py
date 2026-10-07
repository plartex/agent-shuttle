"""Contract tests for the library task components."""

import asyncio
import contextlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_shuttle.task_library import TaskManager
from agent_shuttle.library_repository import LibraryTaskRepository
from agent_shuttle.event_stream import EventStreamService
from agent_shuttle.worker_lifecycle import WorkerLifecycleService


class Backend:
    def __init__(self):
        self.calls = 0
        self.release = asyncio.Event()

    async def run(self, prompt, model=None, *, reasoning_effort=None,
                  read_only=False, tool_policy=None, on_event=None):
        self.calls += 1
        if on_event:
            await on_event({"kind": "progress", "text": "working"})
        if prompt == "wait":
            await self.release.wait()
        return "done"


class ComponentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "library.sqlite3"
        self.backend = Backend()

    def manager(self):
        return TaskManager({"fake": self.backend}, workspace=self.root,
                           database=self.path, stall_timeout_seconds=0.05)

    async def test_manager_assembles_components_without_owning_sqlite_or_workers(self):
        async with self.manager() as manager:
            self.assertIsInstance(manager.repository, LibraryTaskRepository)
            self.assertIsInstance(manager.event_stream, EventStreamService)
            self.assertIsInstance(manager.workers, WorkerLifecycleService)
            self.assertNotIn("_conn", vars(manager))
            self.assertNotIn("_active", vars(manager))

    async def test_event_is_durable_before_sink_and_sink_failure_is_detached(self):
        seen = []

        async def sink(event):
            task = await manager.get(task_id)
            seen.append([item["kind"] for item in (await task.transcript())["items"]])
            raise ConnectionError("subscriber left")

        async with self.manager() as manager:
            task_id = "00000000-0000-4000-8000-000000000001"
            task = await manager.dispatch("fake", "hello", task_id=task_id, event_sink=sink)
            self.assertEqual((await task.result()).state, "completed")
            self.assertEqual(seen, [["submitted", "working", "progress"]])
            self.assertEqual([event["kind"] async for event in task.events()][-1], "completed")

    async def test_repository_rolls_back_task_when_initial_event_cannot_be_written(self):
        repository = LibraryTaskRepository(self.path)
        repository.open()
        self.addCleanup(repository.close)
        with self.assertRaises(TypeError):
            repository.create_task({
                "id": "task-1", "agent_id": "fake", "session_id": None,
                "request_id": None, "fingerprint": "fingerprint", "prompt": "hello",
                "model": None, "reasoning_effort": None, "tool_policy": None,
                "output_schema": None,
            }, {"invalid": {("unsupported-key",): 1}})
        self.assertIsNone(repository.find_task("task-1"))
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    async def test_repository_rolls_back_state_when_transition_event_fails(self):
        repository = LibraryTaskRepository(self.path)
        repository.open()
        self.addCleanup(repository.close)
        repository.create_task({
            "id": "task-1", "agent_id": "fake", "prompt": "hello",
        }, {"prompt": "hello"})
        with self.assertRaises(TypeError):
            repository.update_task("task-1", "completed", {"invalid": {("key",): 1}}, text="done")
        self.assertEqual(repository.task("task-1")["state"], "submitted")
        self.assertEqual(repository.task("task-1")["text"], "")
        self.assertEqual(repository.event_count("task-1"), 1)

    async def test_repository_opens_legacy_library_schema_without_losing_data(self):
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            connection.executescript("""
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, session_id TEXT,
                    request_id TEXT UNIQUE, fingerprint TEXT, prompt TEXT NOT NULL,
                    model TEXT, reasoning_effort TEXT, tool_policy TEXT,
                    state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    last_activity_at REAL, text TEXT NOT NULL DEFAULT '',
                    usage TEXT, details TEXT, error TEXT, observed_model TEXT,
                    files_changed TEXT, files_changed_state TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, model TEXT,
                    reasoning_effort TEXT, tool_policy TEXT, state TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                INSERT INTO tasks (id,agent_id,prompt,state,created_at,updated_at)
                    VALUES ('old-task','fake','before upgrade','completed',1,1);
            """)
        repository = LibraryTaskRepository(self.path)
        repository.open()
        self.addCleanup(repository.close)
        self.assertEqual(repository.task("old-task")["prompt"], "before upgrade")
        self.assertIn("output_schema", repository.task("old-task"))
        self.assertIn("warnings", repository.task("old-task"))

    async def test_legacy_event_metadata_migrates_once_with_backup(self):
        repository = LibraryTaskRepository(self.path)
        repository.open()
        repository.create_task({"id": "historic", "agent_id": "fake", "prompt": "old"},
                               {"agent_bridge.event": "progress", "text": "agent_bridge.event"})
        repository.close()
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version=0")
            connection.commit()
        repository.open()
        try:
            event = json.loads(repository.event("historic", 0))
            self.assertEqual(event["agent_shuttle.event"], "progress")
            self.assertEqual(event["text"], "agent_bridge.event")
            self.assertEqual(repository.task("historic")["prompt"], "old")
        finally:
            repository.close()
        self.assertEqual(len(list(self.root.glob("library.sqlite3.pre-0.7-*.sqlite3"))), 1)
        repository.open()
        repository.close()
        self.assertEqual(len(list(self.root.glob("library.sqlite3.pre-0.7-*.sqlite3"))), 1)

    async def test_repository_releases_exclusive_owner_lock_on_close(self):
        first = LibraryTaskRepository(self.path)
        second = LibraryTaskRepository(self.path)
        first.open()
        try:
            with self.assertRaises(OSError):
                second.open()
        finally:
            first.close()
        second.open()
        second.close()

    async def test_recovery_does_not_replay_interrupted_task(self):
        async with self.manager() as manager:
            task = await manager.dispatch("fake", "hello")
            await task.result()
            task_id = task.id
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE tasks SET state='working' WHERE id=?", (task_id,))
            connection.commit()
        async with self.manager() as manager:
            result = await manager.result(task_id)
            self.assertEqual(result.error["code"], "worker_interrupted")
            self.assertEqual([item["kind"] for item in (await manager.transcript(task_id))["items"]][-1], "failed")
        self.assertEqual(self.backend.calls, 1)

    async def test_cancel_and_stall_are_owned_by_worker_service(self):
        async with self.manager() as manager:
            task = await manager.dispatch("fake", "wait")
            self.assertEqual((await asyncio.wait_for(task.result(), 2)).error["code"], "worker_stalled")
            second = await manager.dispatch("fake", "wait")
            self.assertEqual((await second.cancel()).state, "canceled")

    async def test_preparation_exhausts_execution_budget_before_backend_starts(self):
        async with TaskManager({"fake": self.backend}, workspace=self.root, memory=True,
                               execution_timeout_seconds=0.05) as manager:
            preparation = asyncio.Event()
            cancelled = asyncio.Event()

            async def slow_snapshot(*args, **kwargs):
                try:
                    await preparation.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            manager.workers._workspace_changes = slow_snapshot
            task = await manager.dispatch("fake", "hello")
            result = await asyncio.wait_for(task.result(), 1)
            self.assertEqual(result.error["code"], "worker_timeout")
            self.assertEqual(self.backend.calls, 0)
            self.assertTrue(cancelled.is_set())
            preparation.set()
            await asyncio.sleep(0)
            self.assertEqual((await task.status()).state, "failed")

    async def test_finished_answer_survives_slow_final_git_snapshot(self):
        async with TaskManager({"fake": self.backend}, workspace=self.root, memory=True,
                               execution_timeout_seconds=0.1) as manager:
            snapshots = 0
            cancelled = asyncio.Event()

            async def snapshot(*args, **kwargs):
                nonlocal snapshots
                snapshots += 1
                if snapshots == 1:
                    return set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            manager.workers._workspace_changes = snapshot
            task = await manager.dispatch("fake", "hello")
            result = await asyncio.wait_for(task.result(), 1)
            self.assertEqual(result.state, "completed")
            self.assertEqual(result.text, "done")
            self.assertEqual(result.files_changed_state, "unavailable")
            self.assertTrue(cancelled.is_set())

    async def test_session_queue_uses_execution_budget_without_interrupting_session(self):
        class Native:
            async def ask(self, prompt):
                return prompt

            async def close(self):
                pass

        class SessionBackend(Backend):
            async def open_session(self, *args, **kwargs):
                return Native()

        async with TaskManager({"fake": SessionBackend()}, workspace=self.root, memory=True,
                               execution_timeout_seconds=0.05) as manager:
            session = await manager.create_session("fake")
            lock = asyncio.Lock()
            await lock.acquire()
            manager.workers._session_locks[session.id] = lock
            try:
                task = await session.dispatch("queued")
                result = await asyncio.wait_for(task.result(), 1)
                self.assertEqual(result.error["code"], "worker_timeout")
                self.assertEqual((await manager.list_sessions())[0].state, "open")
            finally:
                lock.release()
            resumed = await session.dispatch("next")
            self.assertEqual((await asyncio.wait_for(resumed.result(), 1)).text, "next")

    async def test_git_launch_is_inside_its_own_budget(self):
        (self.root / ".git").mkdir()
        async with self.manager() as manager:
            async def slow_launch(*args, **kwargs):
                await asyncio.Event().wait()

            with patch("agent_shuttle.worker_lifecycle.asyncio.create_subprocess_exec", slow_launch):
                result = await asyncio.wait_for(manager.workers._workspace_changes(0.05), 0.5)
            self.assertIsNone(result)

    async def test_git_process_is_reaped_after_its_own_timeout(self):
        (self.root / ".git").mkdir()

        class HungProcess:
            returncode = None

            def __init__(self):
                self.killed = False
                self.reaped = False

            async def communicate(self):
                await asyncio.Event().wait()

            def kill(self):
                self.killed = True

            async def wait(self):
                self.reaped = True
                self.returncode = -9

        process = HungProcess()
        async with self.manager() as manager:
            async def launch(*args, **kwargs):
                return process

            with patch("agent_shuttle.worker_lifecycle.asyncio.create_subprocess_exec", launch):
                result = await asyncio.wait_for(manager.workers._workspace_changes(0.05), 0.5)
            self.assertIsNone(result)
            self.assertTrue(process.killed)
            self.assertTrue(process.reaped)
