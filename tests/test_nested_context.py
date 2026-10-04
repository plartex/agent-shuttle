"""Regression tests for a Bridge inherited by a delegated worker."""

import asyncio
import os
import socket
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from a2a.client import ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import CancelTaskRequest, Role, SendMessageRequest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from agent_shuttle import mcp_server
from agent_shuttle.client import BridgeClient
from agent_shuttle.a2a_server import make_app
from agent_shuttle.backends import AntigravityCliBackend, AntigravitySdkBackend, CodexBackend
from agent_shuttle.task_library import TaskManager
from agent_shuttle.profiles import AgentProfile, ToolPolicy


class FakeBackend:
    def __init__(self):
        self.calls = []

    async def run(self, prompt, model=None, **kwargs):
        self.calls.append(prompt)
        return "done"


class WorkerEnvironmentTests(unittest.TestCase):
    def test_marker_is_exact_and_worker_overrides_cannot_clear_it(self):
        from agent_shuttle.runtime_context import WORKER_CONTEXT_ENV, is_worker_context, worker_env

        self.assertFalse(is_worker_context({}))
        self.assertFalse(is_worker_context({WORKER_CONTEXT_ENV: "coordinator"}))
        self.assertFalse(is_worker_context({WORKER_CONTEXT_ENV: "Worker"}))
        self.assertTrue(is_worker_context({WORKER_CONTEXT_ENV: "worker"}))
        marked = worker_env({WORKER_CONTEXT_ENV: "coordinator", "KEEP": "yes"})
        self.assertEqual(marked[WORKER_CONTEXT_ENV], "worker")
        self.assertEqual(marked["KEEP"], "yes")

    def test_scrubbed_runtimes_keep_worker_marker(self):
        from agent_shuttle.runtime_context import WORKER_CONTEXT_ENV
        from agent_shuttle.claude_runtime import _environment
        from agent_shuttle.opencode_runtime import _child_env

        with tempfile.TemporaryDirectory() as folder:
            def profile(runtime):
                return AgentProfile.from_mapping({
                    "id": runtime, "runtime": runtime, "provider": "ollama",
                    "workspace": folder, "endpoint": "http://127.0.0.1:11434",
                    "default_model": "test", "allowed_models": ["test"],
                    "max_tool_policy": "no_tools",
                })

            with patch.dict(os.environ, {WORKER_CONTEXT_ENV: "coordinator"}):
                self.assertEqual(_environment(profile("claude_code"), folder)[WORKER_CONTEXT_ENV],
                                 "worker")
                self.assertEqual(_child_env(profile("opencode"), ToolPolicy.NO_TOOLS,
                                            "password")[WORKER_CONTEXT_ENV], "worker")


class NestedLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backend = FakeBackend()

    async def test_worker_rejects_all_public_mutations_before_persistence(self):
        from agent_shuttle.runtime_context import NESTED_DISPATCH_ERROR

        database = self.root / "nested.sqlite3"
        async with TaskManager({"fake": self.backend}, workspace=self.root,
                               database=database, runtime_context="worker") as manager:
            actions = (
                manager.dispatch("fake", "new"),
                manager.dispatch("fake", "retry", request_id=str(uuid.uuid4())),
                manager.create_session("fake"),
                manager.ensure_session(str(uuid.uuid4()), "fake"),
                manager.dispatch_session(str(uuid.uuid4()), "follow up"),
                manager.set_preference("fake", model="x"),
                manager.cancel(str(uuid.uuid4())),
                manager.end_session(str(uuid.uuid4())),
            )
            for action in actions:
                with self.subTest(action=action):
                    with self.assertRaisesRegex(RuntimeError, NESTED_DISPATCH_ERROR):
                        await action
            self.assertEqual(await manager.list_tasks(), [])
            self.assertEqual(await manager.list_sessions(), [])
            self.assertEqual(await manager.get_preference("fake"),
                             {"model": None, "reasoning_effort": None})
        self.assertEqual(self.backend.calls, [])

    async def test_worker_uses_separate_default_database(self):
        from agent_shuttle.runtime_context import WORKER_CONTEXT_ENV

        with patch.dict(os.environ, {WORKER_CONTEXT_ENV: "worker"}):
            manager = TaskManager({"fake": self.backend}, workspace=self.root)
        self.assertEqual(manager.database,
                         (self.root / ".agent-shuttle" / "nested" / "library-tasks.sqlite3").resolve())

    async def test_worker_marker_cannot_be_downgraded_by_constructor_argument(self):
        from agent_shuttle.runtime_context import WORKER_CONTEXT_ENV

        with patch.dict(os.environ, {WORKER_CONTEXT_ENV: "worker"}):
            manager = TaskManager({"fake": self.backend}, workspace=self.root,
                                  runtime_context="coordinator")
        self.assertEqual(manager.runtime_context, "worker")
        self.assertIn("nested", manager.database.parts)

    async def test_explicit_database_path_is_redirected(self):
        database = self.root / "custom.sqlite3"
        manager = TaskManager({"fake": self.backend}, workspace=self.root,
                              database=database, runtime_context="worker")
        self.assertEqual(manager.database, (self.root / "nested" / database.name).resolve())
        self.assertFalse(database.exists())

    async def test_coordinator_still_dispatches(self):
        async with TaskManager({"fake": self.backend}, workspace=self.root,
                               memory=True, runtime_context="coordinator") as manager:
            task = await manager.dispatch("fake", "new")
            self.assertEqual((await task.result()).state, "completed")
        self.assertEqual(self.backend.calls, ["new"])


class WorkerLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_sdk_gets_marker_when_starting_a_worker(self):
        from agent_shuttle.runtime_context import WORKER_CONTEXT_ENV

        class StopAfterCapture(Exception):
            pass

        captured = []

        def capture(config):
            captured.append(config.env)
            raise StopAfterCapture

        with patch.dict(os.environ, {WORKER_CONTEXT_ENV: "coordinator"}), \
             patch("openai_codex.AsyncCodex", side_effect=capture):
            with self.assertRaises(StopAfterCapture):
                await CodexBackend(Path.cwd()).open_session()
        self.assertEqual(captured[0][WORKER_CONTEXT_ENV], "worker")

    async def test_antigravity_cli_gets_marker_in_both_launch_modes(self):
        from agent_shuttle.runtime_context import WORKER_CONTEXT_ENV

        class StopAfterCapture(Exception):
            pass

        captured = []

        async def capture(*args, **kwargs):
            captured.append(kwargs["env"])
            raise StopAfterCapture

        with patch.dict(os.environ, {WORKER_CONTEXT_ENV: "coordinator"}), \
             patch("agent_shuttle.backends.asyncio.create_subprocess_exec",
                   side_effect=capture):
            backend = AntigravityCliBackend(Path.cwd())
            with self.assertRaises(StopAfterCapture):
                await backend.run("one shot")
            with self.assertRaises(StopAfterCapture):
                await backend.open_session()
        self.assertEqual([env[WORKER_CONTEXT_ENV] for env in captured], ["worker", "worker"])

    async def test_antigravity_sdk_helper_gets_marker(self):
        from agent_shuttle.runtime_context import WORKER_CONTEXT_ENV

        class StopAfterCapture(Exception):
            pass

        captured = []

        async def capture(*args, **kwargs):
            captured.append(kwargs["env"])
            raise StopAfterCapture

        with tempfile.TemporaryDirectory() as folder:
            python = Path(folder) / "python.exe"
            python.touch()
            with patch.dict(os.environ, {WORKER_CONTEXT_ENV: "coordinator"}), \
                 patch("agent_shuttle.backends.asyncio.create_subprocess_exec",
                       side_effect=capture):
                with self.assertRaises(StopAfterCapture):
                    await AntigravitySdkBackend(Path(folder), python).run("task")
        self.assertEqual(captured[0][WORKER_CONTEXT_ENV], "worker")


class NestedEntryPointTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_client_rejects_before_network(self):
        from agent_shuttle.runtime_context import WORKER_CONTEXT_ENV, NESTED_DISPATCH_ERROR

        client = BridgeClient()
        with patch.dict(os.environ, {WORKER_CONTEXT_ENV: "worker"}), \
             patch("agent_shuttle.client.httpx.AsyncClient") as http:
            for call in (
                client.submit("http://127.0.0.1:1", "prompt"),
                client.ask("http://127.0.0.1:1", "prompt"),
                client.cancel_task("http://127.0.0.1:1", str(uuid.uuid4())),
                client.close_session("http://127.0.0.1:1", str(uuid.uuid4())),
            ):
                with self.assertRaisesRegex(RuntimeError, NESTED_DISPATCH_ERROR):
                    await call
            http.assert_not_called()

    async def test_mcp_mutations_reject_before_gateway_or_peer(self):
        from agent_shuttle.runtime_context import WORKER_CONTEXT_ENV, NESTED_DISPATCH_ERROR

        with patch.dict(os.environ, {WORKER_CONTEXT_ENV: "worker"}), \
             patch.object(mcp_server, "_gateway") as gateway, \
             patch.object(mcp_server, "connect_harness") as connect:
            for call in (
                mcp_server.ask_agent("codex", "prompt"),
                mcp_server.ask_codex("prompt"),
                mcp_server.ask_antigravity("prompt"),
                mcp_server.submit_task("codex", "prompt"),
                mcp_server.cancel_task(str(uuid.uuid4())),
            ):
                with self.assertRaisesRegex(RuntimeError, NESTED_DISPATCH_ERROR):
                    await call
            gateway.assert_not_called()
            connect.assert_not_called()

    async def test_nested_mcp_registry_path_is_separate(self):
        from agent_shuttle.runtime_context import WORKER_CONTEXT_ENV

        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {WORKER_CONTEXT_ENV: "worker",
                                  "BRIDGE_WORKSPACE": folder,
                                  "BRIDGE_TASK_REGISTRY": str(Path(folder) / "tickets.json")}):
            with patch.object(mcp_server, "_task_gateway", None):
                self.assertEqual(mcp_server._gateway().path,
                                 (Path(folder) / "nested" / "tickets.json").resolve())

    async def test_cli_redirects_explicit_a2a_database_before_opening_it(self):
        from agent_shuttle.cli import main
        from agent_shuttle.runtime_context import WORKER_CONTEXT_ENV

        with tempfile.TemporaryDirectory() as folder:
            parent_database = Path(folder) / "tasks.sqlite3"
            argv = ["agent-shuttle", "serve", "codex", "--workspace", folder,
                    "--port", "8765", "--task-db", str(parent_database)]
            with patch.dict(os.environ, {WORKER_CONTEXT_ENV: "worker"}), \
                 patch("sys.argv", argv), \
                 patch("agent_shuttle.cli.uvicorn.run") as run:
                main()
            self.assertFalse(parent_database.exists())
            self.assertTrue((Path(folder) / "nested" / parent_database.name).exists())
            self.assertEqual(run.call_args.args[0].state.task_manager.runtime_context, "worker")

    async def test_direct_sqlite_task_store_is_isolated_in_worker(self):
        from agent_shuttle.runtime_context import WORKER_CONTEXT_ENV
        from agent_shuttle.task_store import SQLiteTaskStore

        with tempfile.TemporaryDirectory() as folder:
            parent_database = Path(folder) / "tasks.sqlite3"
            with patch.dict(os.environ, {WORKER_CONTEXT_ENV: "worker"}):
                store = SQLiteTaskStore(parent_database)
            try:
                self.assertEqual(store.path, (Path(folder) / "nested" / parent_database.name).resolve())
                self.assertFalse(parent_database.exists())
            finally:
                store.close()

    async def test_nested_stdio_mcp_rejects_without_creating_parent_tickets(self):
        from agent_shuttle.runtime_context import WORKER_CONTEXT_ENV

        with tempfile.TemporaryDirectory() as folder:
            parent_tickets = Path(folder) / ".agent-shuttle" / "mcp-tasks.json"
            parent_tickets.parent.mkdir()
            parent_tickets.write_text('{"version":1,"jobs":{}}', encoding="utf-8")
            before = parent_tickets.read_bytes()
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "agent_shuttle.mcp_server"],
                env={**os.environ, WORKER_CONTEXT_ENV: "worker",
                     "BRIDGE_WORKSPACE": folder, "BRIDGE_TASK_REGISTRY": str(parent_tickets)},
            )
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    for name, arguments in (
                        ("ask_agent", {"agent_id": "codex", "prompt": "nested"}),
                        ("submit_task", {"agent_id": "codex", "prompt": "nested"}),
                        ("cancel_task", {"task_id": str(uuid.uuid4())}),
                    ):
                        with self.subTest(name=name):
                            result = await session.call_tool(name, arguments)
                            self.assertTrue(result.isError)
                            self.assertIn("nested_dispatch_disabled", str(result.content))
            self.assertEqual(parent_tickets.read_bytes(), before)
            self.assertFalse((parent_tickets.parent / "nested" / parent_tickets.name).exists())

    async def test_a2a_transport_rejects_send_cancel_and_close(self):
        from agent_shuttle.runtime_context import WORKER_CONTEXT_ENV

        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {WORKER_CONTEXT_ENV: "worker"}):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            backend = FakeBackend()
            backend.workspace = Path(folder)
            app = make_app("fake", backend, url)
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
                    identity = (await http.get(url + "/bridge/identity")).json()
                    self.assertEqual(identity["runtime_context"], "worker")
                    self.assertFalse(identity["dispatch_enabled"])
                    self.assertEqual((await http.delete(url + "/bridge/sessions/" +
                                                        str(uuid.uuid4()))).status_code, 403)
                    client = await create_client(url, ClientConfig(streaming=False, httpx_client=http))
                    try:
                        message = new_text_message("nested", role=Role.ROLE_USER)
                        with self.assertRaisesRegex(Exception, "nested_dispatch_disabled"):
                            async for _ in client.send_message(SendMessageRequest(message=message)):
                                pass
                        with self.assertRaisesRegex(Exception, "nested_dispatch_disabled"):
                            await client.cancel_task(CancelTaskRequest(id=str(uuid.uuid4())))
                        with patch.dict(os.environ, {WORKER_CONTEXT_ENV: ""}):
                            self.assertEqual((await http.delete(url + "/bridge/sessions/" +
                                                                str(uuid.uuid4()))).status_code, 403)
                            with self.assertRaisesRegex(Exception, "nested_dispatch_disabled"):
                                async for _ in client.send_message(SendMessageRequest(
                                        message=new_text_message("still nested", role=Role.ROLE_USER))):
                                    pass
                    finally:
                        await client.close()
                    self.assertEqual(await app.state.task_manager.list_tasks(), [])
                    self.assertEqual(backend.calls, [])
            finally:
                server.should_exit = True
                await running


if __name__ == "__main__":
    unittest.main()
