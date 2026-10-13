"""Web research is an explicit native-tool scope, not unrestricted execution."""
import json
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

from agent_shuttle.agy_policy import ScopedAgyPolicy, evaluate_tool_call
from agent_shuttle.backends import AntigravityCliBackend
from agent_shuttle.mcp_server import _agent_launch, ask_agent, mcp
from agent_shuttle.profiles import AgentProfile, ToolPolicy
from agent_shuttle.managed import HarnessLaunch, connect_harness


class WebResearchTests(unittest.TestCase):
    def test_native_web_tools_are_allowed_without_local_file_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            for name, args in (
                ("search_web", {"Query": "official MCP documentation"}),
                ("read_url_content", {"Url": "https://modelcontextprotocol.io"}),
                ("view_content_chunk", {"DocumentId": "retrieved-page", "Position": 0}),
            ):
                with self.subTest(name=name):
                    result = evaluate_tool_call("web_research", Path(folder), {"name": name, "args": args})
                    self.assertEqual(result["decision"], "allow")
                    self.assertNotIn("permissionOverrides", result)
                    self.assertEqual(evaluate_tool_call("read_only", Path(folder), {"name": name, "args": args})["decision"], "deny")

    def test_web_scope_does_not_grant_local_tools_or_arbitrary_mcp(self):
        with tempfile.TemporaryDirectory() as folder:
            for tool in ("view_file", "list_dir", "grep_search", "write_to_file", "run_command",
                         "browser_subagent", "invoke_subagent", "ask_permission", "mcp_search_web"):
                self.assertEqual(evaluate_tool_call("web_research", Path(folder), {
                    "name": tool, "args": {"AbsolutePath": folder, "Query": "test"},
                })["decision"], "deny")

    def test_rejects_non_web_urls_and_malformed_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            calls = [("read_url_content", {"Url": url}) for url in (
                "file:///etc/passwd", "C:/secret", "javascript:alert(1)", "https://user:pass@example.com", "https://", None)]
            calls += [("view_content_chunk", {"DocumentId": "page", "Position": True})]
            for name, args in calls:
                self.assertEqual(evaluate_tool_call("web_research", Path(folder), {"name": name, "args": args})["decision"], "deny")

    def test_hook_enforces_both_sides_after_verified_probe(self):
        with tempfile.TemporaryDirectory() as folder, ScopedAgyPolicy("web_research", Path(folder)) as scoped:
            def call(name, args):
                output = subprocess.run(scoped.hook_argv, input=json.dumps({"toolCall": {"name": name, "args": args}}),
                    text=True, capture_output=True, check=True)
                return json.loads(output.stdout)["decision"]
            self.assertEqual(call("write_to_file", {"TargetFile": str(scoped.canary)}), "deny")
            scoped.verify_probe()
            self.assertEqual(call("search_web", {"Query": "MCP"}), "allow")
            self.assertEqual(call("read_url_content", {"Url": "https://example.com"}), "allow")
            self.assertEqual(call("run_command", {"CommandLine": "echo forbidden"}), "deny")
            self.assertFalse(scoped.canary.exists())

    def test_mcp_routes_explicit_policies_without_changing_default(self):
        self.assertEqual(_agent_launch("antigravity", tool_policy="web_research").tool_policy, "web_research")
        self.assertEqual(_agent_launch("antigravity", tool_policy="full_access").tool_policy, "full_access")
        self.assertEqual(_agent_launch("antigravity").tool_policy, "read_only")
        self.assertEqual(ToolPolicy("web_research"), ToolPolicy.WEB_RESEARCH)
        self.assertEqual(AntigravityCliBackend(Path.cwd())._tool_policy(False, "web_research"), "web_research")
        self.assertIn("web_research", mcp.instructions)

    def test_profiles_cannot_silently_claim_native_antigravity_web_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            base = {"id": "acp", "runtime": "acp", "workspace": folder, "command": ["fake"]}
            with self.assertRaisesRegex(ValueError, "built-in Antigravity"):
                AgentProfile.from_mapping({**base, "max_tool_policy": "web_research"})
            profile = AgentProfile.from_mapping(base)
            with self.assertRaisesRegex(ValueError, "built-in Antigravity"):
                profile.resolve(None, None, "web_research")


class McpPolicyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_access_and_research_reach_mcp_dispatch_unchanged(self):
        for policy in ("full_access", "web_research"):
            with patch("agent_shuttle.mcp_server._managed_ask", new_callable=AsyncMock, return_value={"ok": True}) as send:
                await ask_agent("antigravity", "test", tool_policy=policy)
                self.assertEqual(send.await_args.args[0].tool_policy, policy)

    async def test_web_policy_is_rejected_before_starting_other_harnesses(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "built-in Antigravity"):
                async with connect_harness(HarnessLaunch("codex", "http://127.0.0.1:8765", Path(folder), tool_policy="web_research")):
                    self.fail("unsupported harness started")
