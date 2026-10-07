"""Structured output must reach the native CLI through library and A2A."""

import asyncio
import json
import tempfile
import socket
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import AsyncMock
import httpx
import uvicorn

from agent_shuttle.backends import AntigravityCliBackend, _AntigravityCliSession
from agent_shuttle.backends import AntigravityPermissionDenied
from agent_shuttle.agy_policy import ScopedAgyPolicy
from agent_shuttle.task_library import TaskManager
from agent_shuttle.client import ShuttleClient
from agent_shuttle.a2a_server import make_app
from tests.test_backends import _FakeProcess


SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}},
          "required": ["answer"], "additionalProperties": False}


class StructuredOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_timeout_retries_only_the_probe_with_model_and_policy_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            spawned = []
            def spawn(*argv, **kwargs):
                spawned.append((argv, Path(kwargs["cwd"])))
                return _FakeProcess([])
            with patch("agent_shuttle.backends.asyncio.create_subprocess_exec", side_effect=spawn), \
                 patch.object(_AntigravityCliSession, "verify_policy", new=AsyncMock(
                     side_effect=[TimeoutError("probe stalled"), None])):
                session = await AntigravityCliBackend(Path(folder)).open_session(
                    "gemini-3.8-flash-high", reasoning_effort="high", tool_policy="read_only",
                )
                await session.close()
            self.assertEqual(len(spawned), 2)
            self.assertNotEqual(spawned[0][1], spawned[1][1])
            for argv, control in spawned:
                self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-high")
                self.assertEqual(argv[argv.index("--effort") + 1], "high")
                self.assertFalse(control.exists())

    async def test_startup_probe_has_a_separate_short_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            with ScopedAgyPolicy("read_only", Path(folder)) as policy:
                session = _AntigravityCliSession(_FakeProcess([]), policy=policy,
                                                 turn_timeout_seconds=900)
                async def stalled(*args, **kwargs):
                    await asyncio.Event().wait()
                session._ask_within_deadline = stalled
                session.startup_timeout_seconds = .01
                try:
                    started = asyncio.get_running_loop().time()
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(session.verify_policy(), .1)
                    self.assertLess(asyncio.get_running_loop().time() - started, .08)
                finally:
                    await session.close()

    async def test_scoped_structured_result_requires_a_fresh_finish_call(self):
        for fresh_output in (None, {"answer": 42}):
            with self.subTest(fresh_output=fresh_output), tempfile.TemporaryDirectory() as folder:
                with ScopedAgyPolicy("no_tools", Path(folder), output_schema=SCHEMA) as policy:
                    policy.audit.write_text(json.dumps({"tool": "finish", "decision": "allow",
                                                        "output": {"answer": 0}}) + "\n")
                    process = _FakeProcess([{"event": "result", "result": {
                        "status": "SUCCESS", "response": "current answer",
                        "structured_output": {"answer": 0}, "usage": {"total_tokens": 7},
                    }}])
                    async def drain():
                        if fresh_output is not None:
                            with policy.audit.open("a") as log:
                                log.write(json.dumps({"tool": "finish", "decision": "allow",
                                                      "output": fresh_output}) + "\n")
                    process.stdin.drain = drain
                    session = _AntigravityCliSession(process, policy=policy, output_schema=SCHEMA)
                    try:
                        with self.assertRaisesRegex(ValueError, "fresh structured result") as caught:
                            await session.ask("return 42")
                        self.assertEqual(caught.exception.usage, {"total_tokens": 7})
                    finally:
                        await session.close()

    async def test_policy_probe_usage_is_attached_once_to_the_first_user_turn(self):
        process = _FakeProcess([
            {"event": "result", "result": {"status": "SUCCESS", "response": '{"answer":42}',
                                              "usage": {"total_tokens": 17}}},
            {"event": "result", "result": {"status": "SUCCESS", "response": '{"answer":42}',
                                              "usage": {"total_tokens": 23}}},
        ])
        session = _AntigravityCliSession(process, output_schema=SCHEMA)
        session.probe_usage = {"total_tokens": 10}
        session.previous_usage = {"total_tokens": 10}
        try:
            first = await session.ask("first")
            second = await session.ask("second")
            self.assertEqual(first.usage, {"total_tokens": 7})
            self.assertEqual(first.details["policy_probe_usage"], {"total_tokens": 10})
            self.assertEqual(second.usage, {"total_tokens": 6})
            self.assertNotIn("policy_probe_usage", second.details)
        finally:
            await session.close()

    async def test_a2a_transfers_and_pins_output_schema_across_two_turns(self):
        received = []

        class Native:
            async def ask(self, prompt):
                return '{"answer": 42}'

            async def close(self):
                pass

        class Backend:
            async def open_session(self, model=None, *, reasoning_effort=None,
                                   read_only=False, output_schema=None):
                received.append(output_schema)
                return Native()

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(uvicorn.Config(make_app("test", Backend(), url, publish_credential=True),
                                               host="127.0.0.1", port=port, log_level="error"))
        running = asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.02)
            else:
                self.fail("test server did not start")
            async with ShuttleClient().session(url, output_schema=SCHEMA) as session:
                for prompt in ("first", "second"):
                    result = await session.ask(prompt)
                    self.assertEqual(result.state, "TASK_STATE_COMPLETED", result.text)
                    self.assertEqual(json.loads(result.text), {"answer": 42})
        finally:
            server.should_exit = True
            await running
        self.assertEqual(received, [SCHEMA])

    async def test_scoped_native_schema_keeps_permission_probe_and_user_schema_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            def spawn(*argv, **kwargs):
                control = Path(kwargs["cwd"])
                schema_path = Path(argv[argv.index("--json-schema") + 1])
                self.assertEqual(schema_path.parent, control)
                self.assertEqual(json.loads(schema_path.read_text(encoding="utf-8")), SCHEMA)
                (control / "decisions.jsonl").write_text(json.dumps({
                    "tool": "write_to_file", "decision": "deny",
                    "target": str(control / "policy-canary.txt"),
                }) + "\n")
                return _FakeProcess([
                    {"event": "result", "result": {"status": "SUCCESS", "response": '{"answer":0}'}},
                    {"event": "result", "result": {"status": "SUCCESS", "response": '{"answer":42}'}},
                ])

            with patch("agent_shuttle.backends.asyncio.create_subprocess_exec", side_effect=spawn):
                session = await AntigravityCliBackend(Path(folder)).open_session(
                    tool_policy="read_only", output_schema=SCHEMA,
                )
                try:
                    async def drain():
                        with session.policy.audit.open("a") as log:
                            log.write(json.dumps({"tool": "finish", "decision": "allow",
                                                  "output": {"answer": 42}}) + "\n")
                    session.process.stdin.drain = drain
                    self.assertEqual(json.loads((await session.ask("answer")).text), {"answer": 42})
                finally:
                    await session.close()

    async def test_completed_denied_turn_does_not_destroy_the_native_session(self):
        opened = []

        class Native:
            calls = 0

            async def ask(self, prompt):
                self.calls += 1
                if self.calls == 1:
                    raise AntigravityPermissionDenied("view_file outside workspace")
                return "valid response after correcting the path"

            async def close(self):
                pass

        class Backend:
            async def open_session(self, model=None, *, reasoning_effort=None, read_only=False):
                native = Native()
                opened.append(native)
                return native

        async with TaskManager({"test": Backend()}, workspace=Path.cwd()) as manager:
            session = await manager.create_session("test")
            first = await session.dispatch("read")
            self.assertEqual((await first.result()).state, "failed")
            second = await session.dispatch("corrected read")
            self.assertEqual((await second.result()).state, "completed")
        self.assertEqual(len(opened), 1)

    async def test_session_schema_reaches_cli_and_is_pinned(self):
        schemas = []

        class Session:
            async def ask(self, prompt):
                return '{"answer": 42}'

            async def close(self):
                pass

        class Backend:
            async def open_session(self, model=None, *, reasoning_effort=None,
                                   read_only=False, output_schema=None):
                schemas.append(output_schema)
                return Session()

        async with TaskManager({"test": Backend()}, workspace=Path.cwd()) as manager:
            session = await manager.create_session("test", output_schema=SCHEMA)
            task = await session.dispatch("answer")
            self.assertEqual((await task.result()).state, "completed")
            with self.assertRaisesRegex(ValueError, "pinned"):
                await manager.ensure_session(session.id, "test",
                                             output_schema={"type": "object"})
        self.assertEqual(schemas, [SCHEMA])

    async def test_unsupported_schema_rejected_before_dispatch(self):
        class Backend:
            async def run(self, prompt, model=None, *, reasoning_effort=None, read_only=False):
                raise AssertionError("unsupported request reached worker")

        async with TaskManager({"test": Backend()}, workspace=Path.cwd()) as manager:
            with self.assertRaisesRegex(ValueError, "structured output"):
                await manager.dispatch("test", "answer", output_schema=SCHEMA)

    async def test_native_session_passes_schema_and_validates_final_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = AntigravityCliBackend(Path(folder))
            process = _FakeProcess([{"event": "result", "result": {
                "status": "SUCCESS", "response": '{"answer": "wrong type"}',
                "structured_output": {"answer": "wrong type"},
            }}])
            with patch("agent_shuttle.backends.asyncio.create_subprocess_exec", return_value=process) as spawn:
                session = await backend.open_session(output_schema=SCHEMA)
                argv = spawn.call_args.args
                self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), SCHEMA)
                try:
                    with self.assertRaisesRegex(ValueError, "structured output"):
                        await session.ask("answer")
                finally:
                    await session.close()

    async def test_structured_output_is_used_instead_of_free_text(self):
        process = _FakeProcess([{"event": "result", "result": {
            "status": "SUCCESS", "response": "summary text",
            "structured_output": {"answer": 42},
        }}])
        session = _AntigravityCliSession(process, output_schema=SCHEMA)
        try:
            result = await session.ask("answer")
            self.assertEqual(json.loads(result.text), {"answer": 42})
        finally:
            await session.close()
