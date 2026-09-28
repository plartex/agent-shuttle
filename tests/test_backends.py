import asyncio
import json
import tempfile
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_bridge.backends import (
    AntigravityCliBackend, AntigravitySdkBackend, CodexBackend,
    _AntigravityCliSession, _decode_agy_result, _decode_agy_usage,
)


class AntigravityCliBackendTests(unittest.TestCase):
    @staticmethod
    def decode(payload: dict[str, object], stderr: str = "") -> str:
        return _decode_agy_result(json.dumps(payload).encode(), stderr.encode())

    def test_returns_non_empty_success_response(self) -> None:
        self.assertEqual(self.decode({"status": "SUCCESS", "response": "ok"}), "ok")

    def test_rejects_empty_success_response(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "soft-denied.*ViewFile"):
            self.decode(
                {"status": "SUCCESS", "response": ""},
                'Tool "ViewFile" requires confirmation',
            )

    def test_rejects_partial_success_when_command_was_denied(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "RunCommand"):
            self.decode({
                "status": "SUCCESS", "response": "Partial work completed",
                "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
            })

    def test_rejects_empty_success_with_structured_denial(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ViewFile"):
            self.decode({
                "status": "SUCCESS", "response": "",
                "denied_actions": [{"action": "read_file", "display_name": "ViewFile"}],
            })

    def test_full_permissions_setting_requires_boolean(self) -> None:
        with self.assertRaisesRegex(TypeError, "boolean"):
            AntigravityCliBackend(Path.cwd(), dangerously_skip_permissions="false")

    def test_rejects_invalid_json_with_stderr_context(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid JSON.*backend unavailable"):
            _decode_agy_result(b"not-json", b"backend unavailable")

    def test_extracts_only_nonnegative_token_counters(self) -> None:
        payload = {"usage": {
            "input_tokens": 123, "output_tokens": 7, "total_tokens": 130,
            "thinking_tokens": -1, "cache_read_tokens": True, "other": 99,
        }}
        self.assertEqual(
            _decode_agy_usage(json.dumps(payload).encode()),
            {"input_tokens": 123, "output_tokens": 7, "total_tokens": 130},
        )


class _FakeWriter:
    def __init__(self):
        self.lines = []
        self.closed = False

    def write(self, data):
        self.lines.append(data)

    async def drain(self):
        pass

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, events):
        self.stdin = _FakeWriter()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        for event in events:
            self.stdout.feed_data((json.dumps(event) + "\n").encode())
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = None

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


class AntigravitySessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_turn_timeout_terminates_stalled_session(self):
        class StalledProcess(_FakeProcess):
            def __init__(self):
                super().__init__([])
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.terminated = asyncio.Event()

            async def wait(self):
                await self.terminated.wait()
                return self.returncode

            def kill(self):
                self.returncode = -9
                self.stdout.feed_eof()
                self.stderr.feed_eof()
                self.terminated.set()

        process = StalledProcess()
        session = _AntigravityCliSession(process, turn_timeout_seconds=0.01)
        with self.assertRaisesRegex(TimeoutError, "agy session"):
            await session.ask("hello")
        self.assertEqual(process.returncode, -9)

    async def test_read_only_is_rejected_before_starting_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = AntigravityCliBackend(Path(folder))
            with patch("agent_bridge.backends.asyncio.create_subprocess_exec") as spawn:
                with self.assertRaisesRegex(ValueError, "read-only"):
                    await backend.run("inspect", read_only=True)
                with self.assertRaisesRegex(ValueError, "read-only"):
                    await backend.open_session(read_only=True)
            spawn.assert_not_called()

    async def test_unenforceable_tool_policies_are_rejected_before_starting_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = AntigravityCliBackend(Path(folder))
            with patch("agent_bridge.backends.asyncio.create_subprocess_exec") as spawn:
                for policy in ("no_tools", "read_only", "workspace_write"):
                    with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, policy):
                        await backend.run("inspect", tool_policy=policy)
                    with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, policy):
                        await backend.open_session(tool_policy=policy)
            spawn.assert_not_called()

    async def test_stream_all_permissions_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as folder:
            process = _FakeProcess([])
            with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=process) as spawn:
                session = await AntigravityCliBackend(
                    Path(folder), dangerously_skip_permissions=True,
                ).open_session()
                self.assertIn("--dangerously-skip-permissions", spawn.call_args.args)
                await session.close()

    async def test_stream_turns_and_cumulative_usage_deltas(self):
        process = _FakeProcess([
            {"event": "init", "conversation_id": "test"},
            {"event": "result", "result": {
                "status": "SUCCESS", "response": "first", "usage": {
                    "input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 0,
                    "total_tokens": 110,
                },
            }},
            {"event": "result", "result": {
                "status": "SUCCESS", "response": "second", "usage": {
                    "input_tokens": 130, "output_tokens": 15, "cache_read_tokens": 80,
                    "total_tokens": 145,
                },
            }},
        ])
        session = _AntigravityCliSession(process)
        first = await session.ask("one")
        second = await session.ask("two")
        self.assertEqual(first.text, "first")
        self.assertEqual(second.text, "second")
        self.assertEqual(first.usage["input_tokens"], 100)
        self.assertEqual(second.usage["input_tokens"], 30)
        self.assertEqual(second.usage["cache_read_tokens"], 80)
        self.assertEqual(len(process.stdin.lines), 2)
        self.assertEqual(json.loads(process.stdin.lines[1])["message"]["content"], "two")
        await session.close()
        self.assertTrue(process.stdin.closed)

    async def test_stream_rejects_partial_success_when_command_was_denied(self):
        process = _FakeProcess([{"event": "result", "result": {
            "status": "SUCCESS", "response": "Partial work completed",
            "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
        }}])
        session = _AntigravityCliSession(process)
        try:
            with self.assertRaisesRegex(RuntimeError, "RunCommand"):
                await session.ask("do the full task")
        finally:
            await session.close()


class CodexSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_access_session_uses_sdk_full_access_sandbox(self):
        from openai_codex import Sandbox
        started = []

        class Codex:
            def __init__(self, config):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def thread_start(self, **kwargs):
                started.append(kwargs)
                return object()

        with patch("openai_codex.AsyncCodex", Codex):
            session = await CodexBackend(Path.cwd()).open_session(tool_policy="full_access")
            await session.close()
        self.assertEqual(started[0]["sandbox"], Sandbox.full_access)

    async def test_reuses_one_thread_and_closes_runtime(self):
        runtimes = []

        class FakeThread:
            def __init__(self):
                self.calls = []

            async def run(self, prompt):
                self.calls.append(prompt)
                last = SimpleNamespace(
                    input_tokens=100 + len(self.calls), output_tokens=5,
                    total_tokens=105 + len(self.calls), reasoning_output_tokens=2,
                    cached_input_tokens=50, cache_write_input_tokens=0,
                )
                return SimpleNamespace(
                    final_response=f"turn {len(self.calls)}",
                    usage=SimpleNamespace(last=last),
                )

        class FakeCodex:
            def __init__(self, config):
                self.thread = FakeThread()
                self.started = []
                self.closed = False
                runtimes.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                self.closed = True

            async def thread_start(self, **kwargs):
                self.started.append(kwargs)
                return self.thread

        with patch("openai_codex.AsyncCodex", FakeCodex):
            session = await CodexBackend(Path.cwd()).open_session(
                "test-model", reasoning_effort="low", read_only=True,
            )
            first = await session.ask("one")
            second = await session.ask("two")
            self.assertEqual(first.text, "turn 1")
            self.assertEqual(second.text, "turn 2")
            self.assertEqual(second.usage["input_tokens"], 102)
            self.assertEqual(second.usage["cache_read_tokens"], 50)
            await session.close()
        self.assertEqual(len(runtimes), 1)
        self.assertEqual(len(runtimes[0].started), 1)
        self.assertEqual(runtimes[0].thread.calls, ["one", "two"])
        self.assertTrue(runtimes[0].closed)


class OneShotBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_full_access_maps_to_sdk_sandbox(self):
        from openai_codex import Sandbox
        instances = []

        class Thread:
            async def run(self, prompt):
                return SimpleNamespace(final_response="ok", usage=None)

        class Codex:
            def __init__(self, config):
                instances.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def thread_start(self, **kwargs):
                self.settings = kwargs
                return Thread()

        with patch("openai_codex.AsyncCodex", Codex):
            await CodexBackend(Path.cwd()).run("inspect", tool_policy="full_access")
        self.assertEqual(instances[0].settings["sandbox"], Sandbox.full_access)

    async def test_agy_one_shot_timeout_kills_process(self):
        class StalledProcess:
            returncode = None
            killed = False

            async def communicate(self):
                await asyncio.Event().wait()

            def kill(self):
                self.killed = True
                self.returncode = -9

            async def wait(self):
                return self.returncode

        process = StalledProcess()
        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=process) as spawn:
            with self.assertRaisesRegex(TimeoutError, "agy"):
                await AntigravityCliBackend(
                    Path.cwd(), turn_timeout_seconds=0.01,
                ).run("hello")
        self.assertTrue(process.killed)
        self.assertIn("--print-timeout", spawn.call_args.args)

    async def test_codex_one_shot_returns_last_turn_usage(self):
        class Thread:
            async def run(self, prompt):
                last = SimpleNamespace(input_tokens=8, output_tokens=3, total_tokens=11,
                                       reasoning_output_tokens=1, cached_input_tokens=2,
                                       cache_write_input_tokens=None)
                return SimpleNamespace(final_response="ok", usage=SimpleNamespace(last=last))

        class Codex:
            def __init__(self, config):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def thread_start(self, **kwargs):
                self.settings = kwargs
                return Thread()

        with patch("openai_codex.AsyncCodex", Codex):
            result = await CodexBackend(Path.cwd()).run("hello", "test", reasoning_effort="high")
        self.assertEqual(result.text, "ok")
        self.assertEqual(result.usage["cache_read_tokens"], 2)
        self.assertNotIn("cache_write_tokens", result.usage)

    async def test_antigravity_cli_one_shot_parses_response_and_usage(self):
        class Process:
            returncode = 0

            async def communicate(self):
                return json.dumps({"status": "SUCCESS", "response": "ok", "usage": {
                    "input_tokens": 10, "output_tokens": 2,
                }}).encode(), b""

        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=Process()) as spawn:
            response = await AntigravityCliBackend(Path.cwd()).run(
                "hello", "chosen", reasoning_effort="high",
            )
        self.assertEqual(response.text, "ok")
        self.assertEqual(response.usage["input_tokens"], 10)
        self.assertIn("--model", spawn.call_args.args)
        self.assertIn("--effort", spawn.call_args.args)
        self.assertNotIn("--dangerously-skip-permissions", spawn.call_args.args)

    async def test_antigravity_cli_all_permissions_requires_explicit_opt_in(self):
        class Process:
            returncode = 0

            async def communicate(self):
                return b'{"status":"SUCCESS","response":"ok"}', b""

        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=Process()) as spawn:
            await AntigravityCliBackend(
                Path.cwd(), dangerously_skip_permissions=True,
            ).run("hello")
        self.assertIn("--dangerously-skip-permissions", spawn.call_args.args)

    async def test_antigravity_full_access_request_requires_server_opt_in(self):
        with self.assertRaisesRegex(ValueError, "full_access"):
            await AntigravityCliBackend(Path.cwd()).run("inspect", tool_policy="full_access")

        class Process:
            returncode = 0

            async def communicate(self):
                return b'{"status":"SUCCESS","response":"ok"}', b""

        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=Process()):
            answer = await AntigravityCliBackend(
                Path.cwd(), dangerously_skip_permissions=True,
            ).run("inspect", tool_policy="full_access")
        self.assertEqual(answer.text, "ok")

    async def test_antigravity_cli_nonzero_reports_stderr(self):
        class Process:
            returncode = 2

            async def communicate(self):
                return b"", b"bad model"

        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=Process()):
            with self.assertRaisesRegex(RuntimeError, "bad model"):
                await AntigravityCliBackend(Path.cwd()).run("hello")

    async def test_sdk_requires_worker_and_rejects_effort(self):
        backend = AntigravitySdkBackend(Path.cwd(), Path.cwd() / "nonexistent-python")
        with self.assertRaisesRegex(RuntimeError, "missing"):
            await backend.run("hello")
        with self.assertRaises(NotImplementedError):
            await backend.open_session()

    async def test_sdk_worker_success_and_soft_failure(self):
        class Process:
            def __init__(self, output):
                self.output = output
                self.returncode = 0

            async def communicate(self, payload):
                self.payload = json.loads(payload)
                return self.output, b""

        backend = AntigravitySdkBackend(Path.cwd(), Path(sys.executable))
        success = Process(b'noise\nAGENT_BRIDGE_RESULT={"ok": true, "text": "reply"}\n')
        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=success):
            self.assertEqual(await backend.run("hello", "model"), "reply")
        self.assertEqual(success.payload["model"], "model")

        failure = Process(b'AGENT_BRIDGE_RESULT={"ok": false, "error": "denied"}\n')
        with patch("agent_bridge.backends.asyncio.create_subprocess_exec", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "denied"):
                await backend.run("hello")

    async def test_sdk_effort_rejected_without_starting_worker(self):
        backend = AntigravitySdkBackend(Path.cwd(), Path(sys.executable))
        with patch("agent_bridge.backends.asyncio.create_subprocess_exec") as spawn:
            with self.assertRaisesRegex(RuntimeError, "does not expose reasoning"):
                await backend.run("hello", reasoning_effort="high")
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
