import asyncio
import socket
import unittest

import httpx
import uvicorn

from agent_bridge.a2a_server import make_app
from agent_bridge.client import BridgeClient


class EchoBackend:
    async def run(self, prompt: str, model: str | None = None) -> str:
        return f"echo: {prompt}; model: {model or 'default'}"


class EchoInfo:
    async def fetch(self, *, capabilities=True, usage=True):
        result = {"agent": "codex"}
        if capabilities:
            result["capabilities"] = {"selected_model": "test-model"}
        if usage:
            result["usage"] = {"available": True, "groups": []}
        return result


class BridgeProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_a2a_card_and_task_round_trip(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        app = make_app("codex", EchoBackend(), url, EchoInfo())
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
            self.assertEqual(result.text, "echo: привет; model: default")
            self.assertTrue(result.task_id)
            selected = await BridgeClient().ask(url, "привет", model="chosen-model")
            self.assertEqual(selected.text, "echo: привет; model: chosen-model")
            info = await BridgeClient().info(url)
            self.assertEqual(info["capabilities"]["selected_model"], "test-model")
            self.assertTrue(info["usage"]["available"])
            self.assertNotIn("usage", await BridgeClient().capabilities(url))
            self.assertNotIn("capabilities", await BridgeClient().usage(url))
        finally:
            server.should_exit = True
            await running

    async def test_empty_prompt_is_rejected_locally(self):
        with self.assertRaises(ValueError):
            await BridgeClient().ask("http://127.0.0.1:1", "  ")
        with self.assertRaises(ValueError):
            await BridgeClient().ask("http://127.0.0.1:1", "hello", model=" ")


if __name__ == "__main__":
    unittest.main()
