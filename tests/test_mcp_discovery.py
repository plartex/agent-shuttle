"""Discovery and setup contracts through the public MCP boundary."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from agent_shuttle.agent_catalog import inventory
from agent_shuttle.mcp_server import mcp
from agent_shuttle.mcp_setup import configured_text, diagnose_mcp, setup
from agent_shuttle.mcp_transport import LocalMcpAuth, TOKEN_ENV
from agent_shuttle.presentation import VERSION, REGISTRY_DESCRIPTION
from agent_shuttle.runtime_context import worker_env


class InventoryTests(unittest.TestCase):
    def test_one_invalid_profile_does_not_hide_other_agents_or_claim_authentication(self):
        mapping = {"broken": {"harness": "acp", "profile": "missing.json"},
                   "wrong_type": 123, "invalid id": {}}
        with patch.dict(os.environ, {"AGENT_SHUTTLE_AGENTS_JSON": json.dumps(mapping)}), \
             patch("agent_shuttle.agent_catalog.discover_harnesses", return_value={"codex": "agent-shuttle"}):
            result = inventory()
        items = {item["agent_id"]: item for item in result["agents"]}
        self.assertTrue(items["codex"]["command_available"])
        self.assertEqual(items["broken"]["configuration_status"], "invalid")
        self.assertEqual(items["wrong_type"]["configuration_status"], "invalid")
        self.assertEqual(items["invalid id"]["configuration_status"], "invalid")
        self.assertTrue(all(item["authentication"] == "unverified" for item in items.values()))

    def test_worker_environment_cannot_inherit_transport_credentials(self):
        result = worker_env({TOKEN_ENV: "secret", "AGENT_SHUTTLE_GATEWAY_SECRET_FILE": "private", "KEEP": "yes"})
        self.assertNotIn(TOKEN_ENV, result)
        self.assertNotIn("AGENT_SHUTTLE_GATEWAY_SECRET_FILE", result)
        self.assertEqual(result["KEEP"], "yes")


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_doctor_does_not_claim_a_disabled_entry_is_connected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"mcpServers": {"agent-shuttle": {
                "command": sys.executable, "disabled": True}}}))
            with patch("agent_shuttle.mcp_setup.probe", AsyncMock()) as probe:
                report = await diagnose_mcp(path, "mcp-json")
            self.assertEqual(report["status"], "FAIL")
            self.assertEqual(report["error_code"], "disabled")
            probe.assert_not_called()
            updated = json.loads(configured_text(path.read_text(), "mcp-json", Path(folder)))
            self.assertFalse(updated["mcpServers"]["agent-shuttle"]["disabled"])

    async def test_diagnostics_distinguish_missing_config_missing_command_and_stale_server(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "config.json"
            self.assertEqual((await diagnose_mcp(path, "mcp-json"))["error_code"], "config_missing")
            path.write_text(json.dumps({"mcpServers":{"agent-shuttle":{"command":str(root/'missing.exe')}}}))
            self.assertEqual((await diagnose_mcp(path, "mcp-json"))["error_code"], "command_missing")
            script = root / "stale.py"
            script.write_text('from mcp.server.fastmcp import FastMCP\n'
                              'm=FastMCP("Agent Shuttle")\n'
                              'm._mcp_server.version="0.0.1"\n'
                              'def inventory(): return {"agents":[]}\n'
                              'for name in ["ask_agent","submit_task","get_agent_info","list_agents","get_result"]: m.add_tool(inventory,name=name)\n'
                              'm.run()\n')
            path.write_text(json.dumps({"mcpServers":{"agent-shuttle":{"command":sys.executable,"args":[str(script)]}}}))
            result = await diagnose_mcp(path, "mcp-json")
            self.assertEqual(result["stage"], "handshake")
            self.assertEqual(result["error_code"], "version_mismatch")
    async def test_toml_preserves_comments_and_foreign_settings_and_is_idempotent(self):
        original = '# Keep this comment\n[ui]\ncolor = "blue" # retain\n[mcp_servers.other]\ncommand = "other"\n'
        with tempfile.TemporaryDirectory(prefix="шуттл space ") as folder:
            workspace = Path(folder)
            path = workspace / "config.toml"
            path.write_text(original, encoding="utf-8")
            preview = await setup(path, "codex-toml", workspace)
            self.assertFalse(preview["applied"])
            self.assertEqual(path.read_text(), original)
            with patch("agent_shuttle.mcp_setup.probe", AsyncMock(return_value={"version": VERSION})):
                result = await setup(path, "codex-toml", workspace, True)
                updated = path.read_text(encoding="utf-8")
                repeated = await setup(path, "codex-toml", workspace, True)
            self.assertTrue(result["applied"])
            self.assertFalse(repeated["changed"])
            self.assertEqual(path.read_text(encoding="utf-8"), updated)
            self.assertIn('# Keep this comment', updated)
            self.assertIn('color = "blue" # retain', updated)
            self.assertIn('command = "other"', updated)
            self.assertEqual(len(list(workspace.glob("*.shuttle-backup-*"))), 1)

    async def test_json_setup_rollback_on_failed_handshake_and_preserves_other_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mcp.json"
            before = b'{"mcpServers":{"other":{"command":"foreign"}},"custom":true}'
            path.write_bytes(before)
            with patch("agent_shuttle.mcp_setup.probe", AsyncMock(side_effect=ValueError("failed"))):
                with self.assertRaises(ValueError):
                    await setup(path, "mcp-json", Path(folder), True)
            self.assertEqual(path.read_bytes(), before)
            data = json.loads(configured_text(before.decode(), "mcp-json", Path(folder)))
            self.assertEqual(data["mcpServers"]["other"]["command"], "foreign")
            self.assertTrue(data["custom"])

    async def test_invalid_configuration_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.toml"
            path.write_text("[unterminated")
            with self.assertRaises(ValueError):
                await setup(path, "codex-toml", Path(folder), True)
            self.assertEqual(path.read_text(), "[unterminated")

    async def test_real_setup_and_doctor_initialize_without_an_agent_turn(self):
        with tempfile.TemporaryDirectory(prefix="mcp space ") as folder:
            path = Path(folder) / "mcp.json"
            result = await setup(path, "mcp-json", Path(folder), True)
            self.assertEqual(result["server"]["version"], VERSION)
            report = await diagnose_mcp(path, "mcp-json")
            self.assertEqual(report["status"], "OK")
            self.assertEqual(report["harness_readiness"], "unverified")

    async def test_tools_advertise_intent_and_parameter_meaning(self):
        tools = {item.name: item for item in await mcp.list_tools()}
        for name in ("ask_agent", "submit_task"):
            for term in ("Antigravity", "Codex", "Claude Code", "OpenCode", "research", "review", "second opinion"):
                self.assertIn(term, tools[name].description)
            for param in ("agent_id", "prompt", "model", "reasoning_effort", "tool_policy", "workspace"):
                self.assertTrue(tools[name].inputSchema["properties"][param].get("description"))
        for param in ("request_id", "workspace_mode"):
            self.assertTrue(tools["submit_task"].inputSchema["properties"][param].get("description"))
        self.assertLessEqual(len(REGISTRY_DESCRIPTION), 100)


class HttpAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_origin_and_token_are_checked_before_routes(self):
        downstream = AsyncMock()
        app = LocalMcpAuth(downstream, "x" * 40, 9876)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9876") as client:
            self.assertEqual((await client.get("/health")).status_code, 401)
            headers = {"Authorization": "Bearer " + "x" * 40}
            self.assertEqual((await client.get("/health", headers={**headers, "Host": "attacker"})).status_code, 421)
            self.assertEqual((await client.get("/health", headers={**headers, "Origin": "http://attacker"})).status_code, 403)
            response = await client.get("/health", headers=headers)
            self.assertEqual(response.json()["version"], VERSION)
            self.assertEqual((await client.get("/mcp", headers={"Authorization": "wrong"})).status_code, 401)
        downstream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
