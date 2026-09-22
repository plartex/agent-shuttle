import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_bridge.mcp_server import _agent_url, ask_agent, get_agent_info
from agent_bridge.mcp_server import ask_codex, ask_antigravity, get_codex_info, get_antigravity_info


class McpRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_agent_tool_routes_configured_profile(self):
        result = SimpleNamespace(task_id="1", context_id=None, state="done", text="ok",
                                 usage={"input_tokens": 2}, details={"cache_status": "unknown"})
        with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": json.dumps({"local": "http://127.0.0.1:8767"})}), \
             patch("agent_bridge.mcp_server.BridgeClient.ask", new_callable=AsyncMock, return_value=result) as ask, \
             patch("agent_bridge.mcp_server.BridgeClient.info", new_callable=AsyncMock,
                   return_value={"agent": "local"}) as info:
            answer = await ask_agent("local", "hello", model="test", tool_policy="no_tools")
            snapshot = await get_agent_info("local")
        self.assertEqual(answer["usage"]["input_tokens"], 2)
        self.assertEqual(snapshot["agent"], "local")
        self.assertEqual(ask.call_args.args[:2], ("http://127.0.0.1:8767", "hello"))
        self.assertEqual(info.call_args.args[0], "http://127.0.0.1:8767")

    async def test_registry_rejects_untrusted_urls_and_names(self):
        with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": '{"remote":"https://example.com"}'}):
            with self.assertRaisesRegex(ValueError, "loopback"):
                _agent_url("remote")
            with self.assertRaisesRegex(ValueError, "agent_id"):
                _agent_url("../remote")
        with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": "not-json"}):
            with self.assertRaisesRegex(ValueError, "JSON object"):
                _agent_url("local")

    async def test_registry_rejects_missing_and_non_string_urls(self):
        for mapping, message in (("[]", "JSON object"), ("{}", "Unknown"),
                                 ('{"local": 1}', "string")):
            with self.subTest(mapping=mapping), patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": mapping}):
                with self.assertRaisesRegex(ValueError, message):
                    _agent_url("local")

    async def test_legacy_tools_preserve_urls_and_result_details(self):
        result = SimpleNamespace(task_id="1", context_id="c", state="done", text="ok",
                                 usage={"input_tokens": 3}, details={"source": "test"})
        with patch.dict(os.environ, {"BRIDGE_CODEX_URL": "http://127.0.0.1:8765",
                                  "BRIDGE_ANTIGRAVITY_URL": "http://127.0.0.1:8766"}), \
             patch("agent_bridge.mcp_server.BridgeClient.ask", new_callable=AsyncMock,
                   return_value=result) as ask, \
             patch("agent_bridge.mcp_server.BridgeClient.info", new_callable=AsyncMock,
                   return_value={"ok": True}) as info:
            codex = await ask_codex("task", "model", "high")
            self.assertEqual(ask.call_args.args[0], "http://127.0.0.1:8765")
            antigravity = await ask_antigravity("task")
            self.assertEqual(ask.call_args.args[0], "http://127.0.0.1:8766")
            self.assertEqual(await get_codex_info(), {"ok": True})
            self.assertEqual(info.call_args.args[0], "http://127.0.0.1:8765")
            self.assertEqual(await get_antigravity_info(), {"ok": True})
            self.assertEqual(info.call_args.args[0], "http://127.0.0.1:8766")
        self.assertEqual(codex["details"], {"source": "test"})
        self.assertEqual(antigravity["text"], "ok")

    async def test_legacy_tools_fail_without_urls(self):
        with patch.dict(os.environ, {}, clear=True):
            for tool in (ask_codex, ask_antigravity):
                with self.assertRaisesRegex(RuntimeError, "Set BRIDGE_"):
                    await tool("task")
            for tool in (get_codex_info, get_antigravity_info):
                with self.assertRaisesRegex(RuntimeError, "Set BRIDGE_"):
                    await tool()


if __name__ == "__main__":
    unittest.main()
