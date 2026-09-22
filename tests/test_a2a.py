import asyncio
import socket
import unittest
import uuid

import httpx
import uvicorn
from a2a.client import ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import Role, SendMessageRequest, TaskState

from agent_bridge.a2a_server import make_app
from agent_bridge.backends import BackendResponse
from agent_bridge.client import BridgeClient


class EchoBackend:
    def __init__(self):
        self.sessions = []

    async def run(
        self,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
    ) -> str:
        text = (
            f"echo: {prompt}; model: {model or 'default'}; "
            f"effort: {reasoning_effort or 'default'}; read_only: {read_only}"
        )
        return BackendResponse(text, {"input_tokens": 123, "output_tokens": 7, "total_tokens": 130})

    async def open_session(self, model=None, *, reasoning_effort=None, read_only=False):
        session = EchoSession(model, reasoning_effort, read_only)
        self.sessions.append(session)
        return session


class EchoSession:
    def __init__(self, model, effort, read_only):
        self.settings = (model, effort, read_only)
        self.turns = 0
        self.closed = False

    async def ask(self, prompt):
        self.turns += 1
        return BackendResponse(
            f"turn {self.turns}: {prompt}",
            {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        )

    async def close(self):
        self.closed = True


class EchoInfo:
    async def fetch(self, *, capabilities=True, usage=True):
        result = {"agent": "codex"}
        if capabilities:
            result["capabilities"] = {"selected_model": "test-model"}
        if usage:
            result["usage"] = {"available": True, "groups": []}
        return result


class PolicyEchoBackend(EchoBackend):
    def __init__(self):
        super().__init__()
        self.policy = None

    async def run(self, prompt, model=None, *, reasoning_effort=None, read_only=False, tool_policy=None):
        self.policy = tool_policy
        return BackendResponse("policy reply", {"input_tokens": 3}, {"cache_status": "unknown"})


class BridgeProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_a2a_rejects_malformed_request_metadata(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        app = make_app("echo", EchoBackend(), url)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
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
                self.assertEqual((await http.get(url + "/bridge/info")).status_code, 503)
                self.assertEqual((await http.delete(url + "/bridge/sessions/not-a-uuid")).status_code, 400)
                client = await create_client(url, ClientConfig(streaming=False, httpx_client=http))
                try:
                    examples = [
                        ("", {}),
                        ("hello", {"agent_bridge.model": ""}),
                        ("hello", {"agent_bridge.reasoning_effort": 1}),
                        ("hello", {"agent_bridge.read_only": "yes"}),
                        ("hello", {"agent_bridge.tool_policy": "invalid"}),
                        ("hello", {"agent_bridge.read_only": True,
                                   "agent_bridge.tool_policy": "workspace_write"}),
                        ("hello", {"agent_bridge.session_id": "not-a-uuid"}),
                        ("hello", {"agent_bridge.session_id": str(uuid.uuid4())}),
                    ]
                    for prompt, metadata in examples:
                        with self.subTest(metadata=metadata):
                            message = new_text_message(prompt, role=Role.ROLE_USER)
                            message.metadata.update(metadata)
                            last = None
                            async for item in client.send_message(SendMessageRequest(message=message)):
                                last = item
                            if not prompt:
                                self.assertEqual(last.WhichOneof("payload"), "message")
                            else:
                                self.assertEqual(TaskState.Name(last.task.status.state),
                                                 "TASK_STATE_REJECTED")
                finally:
                    await client.close()
        finally:
            server.should_exit = True
            await running

    async def test_tool_policy_and_usage_details_cross_a2a(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        backend = PolicyEchoBackend()
        server = uvicorn.Server(uvicorn.Config(
            make_app("policy", backend, url), host="127.0.0.1", port=port, log_level="error"
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
                    self.fail("A2A server did not start")
            result = await BridgeClient().ask(url, "hello", tool_policy="no_tools")
            self.assertEqual(backend.policy, "no_tools")
            self.assertEqual(result.details["cache_status"], "unknown")
            with self.assertRaisesRegex(ValueError, "tool_policy"):
                await BridgeClient().ask(url, "hello", tool_policy="invalid")
        finally:
            server.should_exit = True
            await running

    async def test_a2a_card_and_task_round_trip(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        backend = EchoBackend()
        app = make_app("codex", backend, url, EchoInfo())
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        running = asyncio.create_task(server.serve())
        try:
            async with httpx.AsyncClient() as http:
                for _ in range(100):
                    try:
                        response = await http.get(url + "/.well-known/agent-card.json")
                        if response.status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.03)
                else:
                    self.fail("A2A server did not start")
                card = response.json()
                self.assertEqual(card["name"], "Codex local agent")
            result = await BridgeClient().ask(url, "привет")
            self.assertEqual(result.state, "TASK_STATE_COMPLETED")
            self.assertEqual(
                result.text,
                "echo: привет; model: default; effort: default; read_only: False",
            )
            self.assertTrue(result.task_id)
            self.assertEqual(result.usage, {"input_tokens": 123, "output_tokens": 7, "total_tokens": 130})
            selected = await BridgeClient().ask(url, "привет", model="chosen-model")
            self.assertEqual(
                selected.text,
                "echo: привет; model: chosen-model; effort: default; read_only: False",
            )
            configured = await BridgeClient().ask(
                url,
                "привет",
                model="chosen-model",
                reasoning_effort="high",
            )
            self.assertEqual(
                configured.text,
                "echo: привет; model: chosen-model; effort: high; read_only: False",
            )
            read_only = await BridgeClient().ask(url, "проверка", read_only=True)
            self.assertEqual(
                read_only.text,
                "echo: проверка; model: default; effort: default; read_only: True",
            )
            info = await BridgeClient().info(url)
            self.assertEqual(info["capabilities"]["selected_model"], "test-model")
            self.assertTrue(info["usage"]["available"])
            self.assertNotIn("usage", await BridgeClient().capabilities(url))
            self.assertNotIn("capabilities", await BridgeClient().usage(url))
            async with BridgeClient().session(
                url, model="chosen-model", reasoning_effort="high", read_only=True,
            ) as session:
                first = await session.ask("first")
                second = await session.ask("second")
                self.assertEqual(first.text, "turn 1: first")
                self.assertEqual(second.text, "turn 2: second")
                self.assertEqual(first.context_id, session.id)
                self.assertEqual(second.context_id, session.id)
                self.assertEqual(len(backend.sessions), 1)
                self.assertEqual(
                    backend.sessions[0].settings, ("chosen-model", "high", True)
                )
                self.assertEqual(second.usage["input_tokens"], 10)
            self.assertTrue(backend.sessions[0].closed)
            with self.assertRaisesRegex(RuntimeError, "closed"):
                await session.ask("too late")
        finally:
            server.should_exit = True
            await running

    async def test_empty_prompt_is_rejected_locally(self):
        with self.assertRaises(ValueError):
            await BridgeClient().ask("http://127.0.0.1:1", "  ")
        with self.assertRaises(ValueError):
            await BridgeClient().ask("http://127.0.0.1:1", "hello", model=" ")
        with self.assertRaises(ValueError):
            await BridgeClient().ask("http://127.0.0.1:1", "hello", reasoning_effort=" ")


if __name__ == "__main__":
    unittest.main()
