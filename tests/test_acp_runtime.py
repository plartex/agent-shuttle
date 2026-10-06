import asyncio
import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
import uvicorn

from agent_shuttle.acp_runtime import ADVISORY_WARNING, AcpRuntime, _Client
from agent_shuttle.a2a_server import make_app
from agent_shuttle.client import BridgeClient
from agent_shuttle.profiled import ProfiledBackend, ProfiledInfo
from agent_shuttle.profiles import AgentProfile, ProfileSelection, ToolPolicy
from agent_shuttle.task_library import TaskManager


FIXTURE = Path(__file__).parent / "fixtures" / "fake_acp_worker.py"


@unittest.skipUnless(importlib.util.find_spec("acp"), "ACP SDK is not installed")
class AcpRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def runtime(self, *extra, **profile_changes):
        data = {"id": "fake", "runtime": "acp", "workspace": self.temp.name,
                "command": [sys.executable, str(FIXTURE), *extra],
                "allowed_models": ["small", "large"], "reasoning_efforts": ["low", "high"]}
        data.update(profile_changes)
        return AcpRuntime(AgentProfile.from_mapping(data))

    async def test_discovery_and_streamed_multiturn(self):
        runtime = self.runtime()
        self.assertTrue((await runtime.discover())["command_found"])
        inspected = await runtime.inspect()
        self.assertTrue(inspected["supports_resume_after_restart"])
        self.assertEqual(inspected["config_options"][0]["id"], "model")
        self.assertEqual(inspected["advertised_models"], ["large", "small"])
        info = await ProfiledInfo(runtime.profile, runtime).fetch(usage=False)
        self.assertEqual(info["tool_policy_enforcement"], "advisory")
        self.assertEqual(info["capabilities"]["models"], ["large", "small"])
        self.assertEqual(info["capabilities"]["selected_model"], "small")
        self.assertEqual(info["capabilities"]["reasoning_efforts"], ["high", "low"])
        self.assertTrue(info["supports_resume_after_restart"])
        backend = ProfiledBackend(runtime.profile, runtime)
        events = []
        session = await backend.open_session("large", reasoning_effort="high", tool_policy="read_only")
        self.assertTrue(session.supports_resume)
        try:
            first = await session.ask("one", on_event=lambda event: self._event(events, event))
            second = await session.ask("two")
            self.assertEqual(first.text, "one:large")
            self.assertEqual(second.text, "two:large")
            self.assertIn(ADVISORY_WARNING, first.details["warnings"])
            self.assertTrue(events)
        finally:
            await session.close()
        self.assertIsNotNone(session.process.returncode)

    async def test_library_result_exposes_advisory_warning_and_events(self):
        runtime = self.runtime()
        backend = ProfiledBackend(runtime.profile, runtime)
        async with TaskManager({"fake": backend}, workspace=self.temp.name, memory=True) as manager:
            task = await manager.dispatch("fake", "library", tool_policy="read_only")
            result = await task.result()
            self.assertEqual(result.text, "library:small")
            self.assertIn(ADVISORY_WARNING, result.warnings)
            self.assertTrue((await task.transcript())["items"])
            agent = (await manager.list_agents())[0]
            self.assertNotIn("read_only", agent.tool_policies)

    async def test_acp_warning_and_capabilities_cross_a2a(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        runtime = self.runtime(allowed_models=[], reasoning_efforts=[])
        server = uvicorn.Server(uvicorn.Config(
            make_app("fake", ProfiledBackend(runtime.profile, runtime), url,
                     ProfiledInfo(runtime.profile, runtime), publish_credential=True),
            host="127.0.0.1", port=port, log_level="error",
        ))
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
                    self.fail("ACP A2A server did not start")
            client = BridgeClient()
            identity = await client.identity(url)
            self.assertEqual(identity["tool_policy_enforcement"], "advisory")
            self.assertFalse(identity["read_only_tools"])
            info = await client.info(url)
            self.assertEqual(info["capabilities"]["models"], ["large", "small"])
            result = await client.ask(url, "a2a", model="large", tool_policy="read_only")
            self.assertEqual(result.text, "a2a:large")
            self.assertIn(ADVISORY_WARNING, result.details["warnings"])
        finally:
            server.should_exit = True
            await running

    async def _event(self, events, event):
        events.append(event)

    async def test_resume_only_when_advertised(self):
        runtime = self.runtime()
        selection = runtime.profile.resolve(None, None, None)
        session = await runtime.open_session(selection)
        native_id = session.native_id
        await session.close()
        resumed = await runtime.resume_session(native_id, selection)
        try:
            self.assertEqual(resumed.native_id, native_id)
            self.assertEqual((await resumed.ask("again")).text, "again:small")
        finally:
            await resumed.close()
        unavailable = self.runtime("no_load")
        no_load_info = await ProfiledInfo(unavailable.profile, unavailable).fetch(usage=False)
        self.assertFalse(no_load_info["supports_resume_after_restart"])
        with self.assertRaisesRegex(RuntimeError, "session/load"):
            await unavailable.resume_session(native_id, selection)

    async def test_library_restart_suspends_only_loadable_sessions(self):
        database = Path(self.temp.name) / "tasks.sqlite3"
        for extra, expected in (((), "suspended"), (("no_load",), "interrupted")):
            with self.subTest(extra=extra):
                database.unlink(missing_ok=True)
                runtime = self.runtime(*extra)
                backend = ProfiledBackend(runtime.profile, runtime)
                async with TaskManager({"fake": backend}, workspace=self.temp.name,
                                       database=database) as manager:
                    session = await manager.create_session("fake")
                    first = await session.dispatch("first")
                    self.assertEqual((await first.result()).text, "first:small")
                    session_id = session.id
                reopened_runtime = self.runtime(*extra)
                reopened = ProfiledBackend(reopened_runtime.profile, reopened_runtime)
                async with TaskManager({"fake": reopened}, workspace=self.temp.name,
                                       database=database) as manager:
                    state = (await manager.list_sessions())[0].state
                    self.assertEqual(state, expected)
                    if expected == "suspended":
                        restored = await manager.session(session_id)
                        second = await restored.dispatch("second")
                        self.assertEqual((await second.result()).text, "second:small")
                    else:
                        with self.assertRaises(RuntimeError):
                            await (await manager.session(session_id)).dispatch("second")

    async def test_unadvertised_model_fails_before_prompt(self):
        runtime = self.runtime(allowed_models=["other"])
        with self.assertRaisesRegex(ValueError, "did not advertise"):
            await runtime.open_session(runtime.profile.resolve("other", None, None))
        self.assertFalse(runtime.sessions)

    async def test_optional_allowlists_are_checked_against_acp_config_options(self):
        runtime = self.runtime(allowed_models=[], reasoning_efforts=[])
        selection = runtime.profile.resolve("large", "high", None)
        session = await runtime.open_session(selection)
        try:
            self.assertEqual((await session.ask("chosen")).text, "chosen:large")
        finally:
            await session.close()
        with self.assertRaisesRegex(ValueError, "did not advertise model"):
            await runtime.open_session(runtime.profile.resolve("unknown", None, None))
        with self.assertRaisesRegex(ValueError, "did not advertise effort"):
            await runtime.open_session(runtime.profile.resolve(None, "ultra", None))

    async def test_cancel_and_crash_cleanup(self):
        runtime = self.runtime()
        session = await runtime.open_session(runtime.profile.resolve(None, None, None))
        running = asyncio.create_task(session.ask("wait"))
        waiting = Path(self.temp.name) / "acp-waiting.flag"
        for _ in range(100):
            if waiting.is_file():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(waiting.is_file())
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        cancelled = Path(self.temp.name) / "acp-cancelled.flag"
        for _ in range(100):
            if cancelled.is_file():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(cancelled.is_file())
        await session.close()
        self.assertIsNotNone(session.process.returncode)
        session = await runtime.open_session(runtime.profile.resolve(None, None, None))
        with self.assertRaises(Exception):
            await asyncio.wait_for(session.ask("crash"), timeout=3)
        await session.close()

    async def test_handshake_timeout_reaps_worker(self):
        runtime = self.runtime("hang_init")
        runtime.handshake_timeout_seconds = 0.1
        with self.assertRaises(TimeoutError):
            await runtime.open_session(runtime.profile.resolve(None, None, None))
        self.assertFalse(runtime.sessions)

    async def test_close_stops_child_process_tree(self):
        runtime = self.runtime("spawn_child")
        session = await runtime.open_session(runtime.profile.resolve(None, None, None))
        heartbeat = Path(self.temp.name) / "acp-heartbeat.txt"
        child_pid_path = Path(self.temp.name) / "acp-child.pid"
        try:
            for _ in range(50):
                if heartbeat.is_file():
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(heartbeat.is_file())
            await session.close()
            await asyncio.sleep(0.2)
            stopped_at = heartbeat.read_text(encoding="utf-8")
            await asyncio.sleep(0.35)
            self.assertEqual(heartbeat.read_text(encoding="utf-8"), stopped_at)
        finally:
            await session.close()
            if child_pid_path.is_file():
                may_be_running = not heartbeat.is_file()
                if heartbeat.is_file():
                    last_beat = heartbeat.read_text(encoding="utf-8")
                    await asyncio.sleep(0.2)
                    may_be_running = heartbeat.read_text(encoding="utf-8") != last_beat
                if may_be_running:
                    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(child_pid), "/T", "/F"],
                                       capture_output=True, check=False)
                    else:
                        try:
                            os.kill(child_pid, 9)
                        except ProcessLookupError:
                            pass

    async def test_permission_requests_follow_advisory_policy(self):
        from acp.schema import PermissionOption, ToolCall, ToolCallLocation

        allow = PermissionOption(optionId="yes", name="Allow", kind="allow_once")
        inside = ToolCall(toolCallId="1", title="Edit", locations=[
            ToolCallLocation(path=str(Path(self.temp.name) / "file.txt"))])
        outside = ToolCall(toolCallId="2", title="Edit", locations=[
            ToolCallLocation(path=str(Path(self.temp.name).parent / "file.txt"))])
        for policy in (ToolPolicy.NO_TOOLS, ToolPolicy.READ_ONLY):
            client = _Client(ProfileSelection(None, None, policy), Path(self.temp.name))
            response = await client.request_permission("session", inside, [allow])
            self.assertEqual(response.outcome.outcome, "cancelled")
        client = _Client(ProfileSelection(None, None, ToolPolicy.WORKSPACE_WRITE),
                         Path(self.temp.name))
        accepted = await client.request_permission("session", inside, [allow])
        denied = await client.request_permission("session", outside, [allow])
        self.assertEqual(accepted.outcome.outcome, "selected")
        self.assertEqual(denied.outcome.outcome, "cancelled")


if __name__ == "__main__":
    unittest.main()
