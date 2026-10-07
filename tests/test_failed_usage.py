"""A completed failed A2A turn must not lose its token ledger."""
import asyncio
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

import uvicorn
from agent_shuttle.a2a_server import make_app
from agent_shuttle.backends import AntigravityCliBackend, AntigravityPermissionDenied, BackendResponse
from agent_shuttle.client import ShuttleClient


class FailedUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_oneshot_combined_usage_is_marked_to_prevent_double_counting_probe(self):
        class Native:
            probe_usage = {"total_tokens":10}
            async def ask(self, prompt, **kwargs):
                return BackendResponse("answer", {"total_tokens":13},
                                       {"policy_probe_usage":{"total_tokens":10}})
            async def close(self):
                pass
        with patch.object(AntigravityCliBackend, "open_session", new=AsyncMock(return_value=Native())):
            result = await AntigravityCliBackend(Path.cwd()).run("answer", tool_policy="no_tools")
        self.assertEqual(result.usage, {"total_tokens":23})
        self.assertTrue(result.details.get("usage_includes_policy_probe"))

    async def test_failed_turn_preserves_usage_details_and_reusable_session_over_a2a(self):
        opened = []
        class Native:
            calls = 0
            async def ask(self, prompt):
                self.calls += 1
                if self.calls == 1:
                    error = AntigravityPermissionDenied("external view_file denied")
                    error.usage = {"input_tokens": 12, "output_tokens": 1, "total_tokens": 13}
                    error.details = {"conversation_id": "native-one",
                                     "policy_probe_usage": {"total_tokens": 2},
                                     "tool_denials": [{"tool": "view_file", "decision": "deny"}]}
                    raise error
                return BackendResponse("corrected answer", {"total_tokens": 6},
                                       {"conversation_id": "native-one"})
            async def close(self):
                pass
        class Backend:
            async def open_session(self, model=None, *, reasoning_effort=None, read_only=False):
                native = Native()
                opened.append(native)
                return native
        with tempfile.TemporaryDirectory() as folder:
            backend = Backend()
            backend.workspace = Path(folder)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            server = uvicorn.Server(uvicorn.Config(make_app("test", backend, url, publish_credential=True),
                host="127.0.0.1", port=port, log_level="error"))
            running = asyncio.create_task(server.serve())
            try:
                for _ in range(100):
                    if server.started:
                        break
                    await asyncio.sleep(.01)
                else:
                    self.fail("A2A server did not start")
                async with ShuttleClient().session(url) as session:
                    failed = await session.ask("first")
                    self.assertEqual(failed.state, "TASK_STATE_FAILED")
                    self.assertTrue(failed.error["retryable"])
                    self.assertEqual(failed.usage, {"input_tokens":12,"output_tokens":1,"total_tokens":13})
                    self.assertEqual(failed.details["conversation_id"], "native-one")
                    self.assertEqual(failed.details["policy_probe_usage"], {"total_tokens":2})
                    second = await session.ask("corrected")
                    self.assertEqual(second.usage, {"total_tokens":6})
                    self.assertNotIn("policy_probe_usage", second.details)
            finally:
                server.should_exit = True
                await running
            self.assertEqual(len(opened), 1)
