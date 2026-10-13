"""Local HTTP parity and opt-in real Docker discovery, without model calls."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import httpx
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from agent_shuttle.mcp_gateway import Adapter, descriptor, _ready
from agent_shuttle.mcp_transport import TOKEN_ENV
from agent_shuttle.process_lifecycle import spawn_options, stop_sync_process
from agent_shuttle.presentation import VERSION


class HttpParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_enforces_disable_for_direct_and_exec_and_keeps_docker_policy(self):
        from mcp.types import Tool, CallToolResult, TextContent
        backend_denial = CallToolResult(content=[TextContent(type="text", text="policy denied")], isError=True)
        docker = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])),
                                 call_tool=AsyncMock(return_value=backend_denial))
        shuttle = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[
            Tool(name="ask_agent", inputSchema={"type": "object"})])), call_tool=AsyncMock())
        adapter = Adapter(docker, shuttle)
        await adapter.tools()
        await adapter.call("mcp-remove", {"name": "agent-shuttle"})
        self.assertTrue((await adapter.call("ask_agent", {})).isError)
        self.assertTrue((await adapter.call("mcp-exec", {"name": "ask_agent", "arguments": {}})).isError)
        shuttle.call_tool.assert_not_called()
        self.assertIs(await adapter.call("docker_owned_tool", {}), backend_denial)
        await adapter.call("mcp-add", {"name": "agent-shuttle"})
        await adapter.call("ask_agent", {})
        shuttle.call_tool.assert_awaited_once_with("ask_agent", {})

    async def test_adapter_rejects_colliding_tool_names(self):
        from mcp.types import Tool
        tools = SimpleNamespace(tools=[Tool(name="ask_agent", inputSchema={"type": "object"})])
        peer = SimpleNamespace(list_tools=AsyncMock(return_value=tools))
        with self.assertRaisesRegex(RuntimeError, "collide"):
            await Adapter(peer, peer).tools()
    async def test_http_and_stdio_have_the_same_tools_and_inventory(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        token = "test-token-" + "x" * 40
        process = subprocess.Popen([sys.executable, "-m", "agent_shuttle.mcp_server", "--transport", "streamable-http", "--port", str(port)],
                                   env={**os.environ, TOKEN_ENV: token}, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, **spawn_options())
        try:
            await _ready(process, port, token)
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as http:
                async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (reader, writer, _):
                    async with ClientSession(reader, writer) as client:
                        initialized = await client.initialize()
                        self.assertEqual(initialized.serverInfo.version, VERSION)
                        remote = (await client.list_tools()).tools
                        result = await client.call_tool("list_agents", {})
                        self.assertFalse(result.isError)
            from agent_shuttle.mcp_server import mcp
            self.assertEqual({item.name: item.inputSchema for item in remote},
                             {item.name: item.inputSchema for item in await mcp.list_tools()})
        finally:
            await stop_sync_process(process)

    def test_catalog_exposes_search_terms_and_no_literal_secret(self):
        data = descriptor(1234)
        for term in ("Antigravity", "Codex", "Claude Code", "OpenCode", "delegate", "delegation", "research", "review", "делегирование", "исследование"):
            self.assertIn(term.lower(), data["description"].lower())
        self.assertEqual(data["remote"]["url"], "http://127.0.0.1:1234/mcp")
        self.assertEqual(data["remote"]["headers"]["Authorization"], "Bearer ${AGENT_SHUTTLE_MCP_TOKEN}")


@unittest.skipUnless(os.environ.get("AGENT_SHUTTLE_LIVE_DOCKER") == "1", "Opt-in actual Docker MCP 0.44.1 test")
class DockerDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_find_connect_call_and_result(self):
        from agent_shuttle.a2a_server import make_app
        from tests.test_a2a import EchoBackend, EchoInfo
        with tempfile.TemporaryDirectory(prefix="shuttle-docker-") as folder:
            root = Path(folder)
            env = {**os.environ, "USERPROFILE": str(root), "HOME": str(root),
                   "DOCKER_CONFIG": str(root / ".docker"), "LOCALAPPDATA": str(root / "appdata"),
                   "AGENT_SHUTTLE_WORKSPACE": str(root)}
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            env["AGENT_SHUTTLE_AGENTS_JSON"] = json.dumps({"smoke": url})
            with patch.dict(os.environ, env):
                app = make_app("smoke", EchoBackend(), url, EchoInfo(), publish_credential=True)
                server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
                running = asyncio.create_task(server.serve())
                try:
                    for _ in range(100):
                        if server.started:
                            break
                        await asyncio.sleep(0.05)
                    created = subprocess.run(["docker", "mcp", "profile", "create", "--name", "fixture"], env=env, capture_output=True, timeout=30)
                    self.assertEqual(created.returncode, 0, created.stderr.decode(errors="replace"))
                    params = StdioServerParameters(command=sys.executable, args=["-m", "agent_shuttle", "mcp", "gateway", "--profile", "fixture"], env=env)
                    async with asyncio.timeout(90):
                        async with stdio_client(params) as (reader, writer):
                            async with ClientSession(reader, writer) as client:
                                await client.initialize()
                                self.assertTrue((await client.list_prompts()).prompts)
                                self.assertTrue((await client.list_resource_templates()).resourceTemplates)
                                for query in ("Antigravity", "Codex", "delegate", "delegation", "research", "review", "делегирование", "исследование"):
                                    found = await client.call_tool("mcp-find", {"query": query, "limit": 10})
                                    self.assertFalse(found.isError, found.content)
                                    self.assertIn("agent-shuttle", " ".join(x.text for x in found.content if x.type == "text"))
                                result = await client.call_tool("ask_agent", {"agent_id": "smoke", "prompt": "discovery smoke"})
                                self.assertFalse(result.isError, result.content)
                                self.assertIn("discovery smoke", " ".join(x.text for x in result.content if x.type == "text"))
                finally:
                    server.should_exit = True
                    await running
