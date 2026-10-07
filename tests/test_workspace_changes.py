"""The isolated artifact includes the complete Git tree and applies explicitly."""

import asyncio
import socket
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent_shuttle.task_library import TaskManager
from agent_shuttle.workspace_changes import GitWorktreeProvider
from agent_shuttle import workspace_changes
from agent_shuttle.library_repository import LibraryTaskRepository
from agent_shuttle.a2a_server import make_app
from agent_shuttle.client import ShuttleClient
from agent_shuttle import mcp_server
import httpx
import uvicorn


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True).stdout.decode().strip()


class WritingBackend:
    enforces_workspace_write = True

    def __init__(self, workspace):
        self.workspace = Path(workspace)

    async def run(self, prompt, model=None, *, reasoning_effort=None, read_only=False,
                  tool_policy=None):
        (self.workspace / "файл.txt").write_text(prompt, encoding="utf-8")
        (self.workspace / "new.bin").write_bytes(b"\x00\xff\x01")
        (self.workspace / "ignored.tmp").write_text("ignored")
        return "written"


class FailingBackend(WritingBackend):
    async def run(self, prompt, model=None, *, reasoning_effort=None, read_only=False,
                  tool_policy=None):
        (self.workspace / "файл.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("worker failed")


class FailCaptureProvider(GitWorktreeProvider):
    def capture(self, change, *, partial):
        raise OSError("capture failed")


class WaitingBackend(WritingBackend):
    started = None

    async def run(self, prompt, model=None, *, reasoning_effort=None, read_only=False,
                  tool_policy=None):
        (self.workspace / "файл.txt").write_text("cancelled", encoding="utf-8")
        self.started.set()
        await asyncio.Event().wait()


class WorkspaceChangesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "проект"
        self.root.mkdir()
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
        (self.root / "файл.txt").write_text("base", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")

    async def test_complete_artifact_and_explicit_apply(self):
        async with TaskManager({"writer": WritingBackend(self.root)}, workspace=self.root,
                               backend_factories={"writer": WritingBackend}) as manager:
            task = await manager.dispatch("writer", "updated", tool_policy="workspace_write",
                                          workspace_mode="isolated")
            self.assertEqual((await task.result()).state, "completed")
            self.assertEqual((self.root / "файл.txt").read_text(encoding="utf-8"), "base")
            change = await task.changes()
            info = await change.info()
            self.assertEqual(info["state"], "ready")
            self.assertEqual(set(info["files"]), {"файл.txt", "new.bin"})
            self.assertIn("файл.txt", (await change.diff())["text"])
            self.assertEqual((await change.apply(expected_revision=info["revision"]))["state"],
                             "applied")
            self.assertEqual((self.root / "файл.txt").read_text(encoding="utf-8"), "updated")
            self.assertEqual((self.root / "new.bin").read_bytes(), b"\x00\xff\x01")
            self.assertFalse((self.root / "ignored.tmp").exists())
            self.assertEqual((await change.apply(expected_revision=info["revision"]))["state"],
                             "already_applied")

    async def test_conflict_preserves_source_and_dirty_checkout_rejected(self):
        async with TaskManager({"writer": WritingBackend(self.root)}, workspace=self.root,
                               backend_factories={"writer": WritingBackend}) as manager:
            task = await manager.dispatch("writer", "updated", tool_policy="workspace_write",
                                          workspace_mode="isolated")
            await task.result()
            change = await task.changes()
            revision = (await change.info())["revision"]
            (self.root / "файл.txt").write_text("other", encoding="utf-8")
            self.assertEqual((await change.apply(expected_revision=revision))["state"], "conflict")
            self.assertEqual((self.root / "файл.txt").read_text(encoding="utf-8"), "other")
            with self.assertRaisesRegex(ValueError, "clean source"):
                await manager.dispatch("writer", "again", tool_policy="workspace_write",
                                       workspace_mode="isolated")

    async def test_backend_must_enforce_workspace_scope(self):
        backend = WritingBackend(self.root)
        backend.enforces_workspace_write = False
        async with TaskManager({"writer": backend}, workspace=self.root,
                               backend_factories={"writer": WritingBackend}) as manager:
            with self.assertRaisesRegex(ValueError, "cannot enforce"):
                await manager.dispatch("writer", "x", tool_policy="workspace_write",
                                       workspace_mode="isolated")

    async def test_failed_task_keeps_partial_artifact(self):
        async with TaskManager({"writer": FailingBackend(self.root)}, workspace=self.root,
                               backend_factories={"writer": FailingBackend}) as manager:
            task = await manager.dispatch("writer", "x", tool_policy="workspace_write",
                                          workspace_mode="isolated")
            self.assertEqual((await task.result()).state, "failed")
            change = await task.changes()
            info = await change.info()
            self.assertEqual(info["state"], "partial")
            with self.assertRaisesRegex(ValueError, "allow_partial"):
                await change.apply(expected_revision=info["revision"])
            self.assertEqual((await change.apply(expected_revision=info["revision"],
                                           allow_partial=True))["state"], "applied")

    async def test_parallel_same_file_conflicts_on_second_apply(self):
        async with TaskManager({"writer": WritingBackend(self.root)}, workspace=self.root,
                               backend_factories={"writer": WritingBackend}) as manager:
            first, second = await asyncio.gather(
                manager.dispatch("writer", "one", tool_policy="workspace_write",
                                 workspace_mode="isolated"),
                manager.dispatch("writer", "two", tool_policy="workspace_write",
                                 workspace_mode="isolated"))
            await asyncio.gather(first.result(), second.result())
            a, b = await first.changes(), await second.changes()
            ar, br = (await a.info())["revision"], (await b.info())["revision"]
            self.assertEqual((await a.apply(expected_revision=ar))["state"], "applied")
            self.assertEqual((await b.apply(expected_revision=br))["state"], "conflict")

    async def test_capture_failure_retains_worktree_for_inspection(self):
        provider = FailCaptureProvider(self.root)
        async with TaskManager({"writer": WritingBackend(self.root)}, workspace=self.root,
                               workspace_provider=provider,
                               backend_factories={"writer": WritingBackend}) as manager:
            task = await manager.dispatch("writer", "x", tool_policy="workspace_write",
                                          workspace_mode="isolated")
            await task.result()
            info = await (await task.changes()).info()
            self.assertEqual(info["state"], "inspection_required")
            self.assertTrue(Path(info["recovery_path"]).exists())
            self.assertIn("capture failed", info["error"])

    async def test_restart_captures_interrupted_worktree(self):
        provider = GitWorktreeProvider(self.root)
        task_id = str(uuid.uuid4())
        change = provider.prepare(task_id)
        (Path(change["workspace_path"]) / "файл.txt").write_text("interrupted", encoding="utf-8")
        repository = LibraryTaskRepository(self.root / ".agent-shuttle" / "library-tasks.sqlite3")
        repository.open()
        try:
            repository.create_task({"id": task_id, "agent_id": "writer", "prompt": "x"}, {})
            repository.save_change(change)
        finally:
            repository.close()
        async with TaskManager({"writer": WritingBackend(self.root)}, workspace=self.root,
                               backend_factories={"writer": WritingBackend}) as manager:
            task = await manager.get(task_id)
            self.assertEqual((await task.result()).state, "failed")
            info = await (await task.changes()).info()
            self.assertEqual(info["state"], "partial")
            self.assertIn("файл.txt", info["files"])

    async def test_cancellation_preserves_partial_changes(self):
        WaitingBackend.started = asyncio.Event()
        async with TaskManager({"writer": WaitingBackend(self.root)}, workspace=self.root,
                               backend_factories={"writer": WaitingBackend}) as manager:
            task = await manager.dispatch("writer", "x", tool_policy="workspace_write",
                                          workspace_mode="isolated")
            await asyncio.wait_for(WaitingBackend.started.wait(), 5)
            self.assertEqual((await task.cancel()).state, "canceled")
            info = await (await task.changes()).info()
            self.assertEqual(info["state"], "partial")
            self.assertIn("файл.txt", info["files"])

    async def test_interrupted_apply_requires_inspection(self):
        async with TaskManager({"writer": WritingBackend(self.root)}, workspace=self.root,
                               backend_factories={"writer": WritingBackend}) as manager:
            task = await manager.dispatch("writer", "updated", tool_policy="workspace_write",
                                          workspace_mode="isolated")
            await task.result()
            change = await task.changes()
            revision = (await change.info())["revision"]
            original_git = workspace_changes._git

            def interrupted_git(root, *args, **kwargs):
                if args[:2] == ("apply", "--binary"):
                    (self.root / "файл.txt").write_text("unknown", encoding="utf-8")
                    return subprocess.CompletedProcess(args, 1, b"", b"interrupted")
                return original_git(root, *args, **kwargs)

            with patch.object(workspace_changes, "_git", side_effect=interrupted_git):
                self.assertEqual((await change.apply(expected_revision=revision))["state"],
                                 "inspection_required")
            with self.assertRaisesRegex(ValueError, "inspection_required"):
                await change.apply(expected_revision=revision)

    async def test_staged_source_divergence_is_conflict(self):
        async with TaskManager({"writer": WritingBackend(self.root)}, workspace=self.root,
                               backend_factories={"writer": WritingBackend}) as manager:
            task = await manager.dispatch("writer", "updated", tool_policy="workspace_write",
                                          workspace_mode="isolated")
            await task.result()
            change = await task.changes()
            revision = (await change.info())["revision"]
            (self.root / "файл.txt").write_text("staged", encoding="utf-8")
            git(self.root, "add", "файл.txt")
            (self.root / "файл.txt").write_text("base", encoding="utf-8")
            self.assertEqual((await change.apply(expected_revision=revision))["state"], "conflict")
            self.assertEqual((self.root / "файл.txt").read_text(encoding="utf-8"), "base")


class ProviderTest(unittest.TestCase):
    def test_no_commit_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            git(root, "init", "-q")
            with self.assertRaises(RuntimeError):
                GitWorktreeProvider(root).prepare(str(uuid.uuid4()))


class TransportTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = WorkspaceChangesTest.asyncSetUp

    async def test_a2a_and_mcp_use_same_artifact(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        app = make_app("writer", WritingBackend(self.root), url, publish_credential=True,
                       backend_factory=WritingBackend)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                              log_level="error"))
        running = asyncio.create_task(server.serve())
        try:
            async with httpx.AsyncClient() as http:
                for _ in range(100):
                    try:
                        if (await http.get(url + "/.well-known/agent-card.json")).status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.03)
                else:
                    self.fail("A2A server did not start")
            client = ShuttleClient(timeout_seconds=5)
            handle = await client.submit(url, "transport", tool_policy="workspace_write",
                                         workspace_mode="isolated")
            self.assertEqual((await handle.result()).state, "TASK_STATE_COMPLETED")
            core = await app.state.task_manager.change_info(handle.task_id)

            class Gateway:
                async def handle(self, task_id):
                    return client.task(url, task_id)

            previous = mcp_server._task_gateway
            mcp_server._task_gateway = Gateway()
            try:
                tool_result = await mcp_server.get_task_changes(handle.task_id)
                info = tool_result.structuredContent
                self.assertEqual(tool_result.content[1].type, "resource_link")
                self.assertEqual(info["revision"], core["revision"])
                self.assertEqual(info["files"], core["files"])
                self.assertIn("файл.txt", (await mcp_server.get_task_diff(handle.task_id))["text"])
                self.assertIn("файл.txt", await mcp_server.task_diff_resource(handle.task_id))
                applied = await mcp_server.apply_task_changes(handle.task_id,
                                                              info["revision"])
                self.assertEqual(applied["state"], "applied")
            finally:
                mcp_server._task_gateway = previous
            self.assertEqual((self.root / "файл.txt").read_text(encoding="utf-8"), "transport")
        finally:
            server.should_exit = True
            await running
