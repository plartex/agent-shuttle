"""Host stdio adapter alongside an unmodified Docker MCP Gateway.

Shuttle is a separate host backend, not a Docker-sandboxed remote server.
Docker continues enforcing policy for every Docker-owned tool invocation.
"""
import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.types import CallToolResult, TextContent

from .mcp_transport import TOKEN_ENV
from .presentation import CATALOG_DESCRIPTION, TITLE, VERSION
from .runtime_context import is_worker_context


def descriptor(port):
    """Manual HTTP descriptor; default Docker policy rejects local remote URLs."""
    return {"name": "agent-shuttle", "title": TITLE, "type": "remote",
            "description": CATALOG_DESCRIPTION,
            "remote": {"transport_type": "streamable-http", "url": f"http://127.0.0.1:{port}/mcp",
                       "headers": {"Authorization": "Bearer ${AGENT_SHUTTLE_MCP_TOKEN}"}}}


async def _ready(process, port, token):
    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Local MCP HTTP server failed to start")
            try:
                response = await client.get(f"http://127.0.0.1:{port}/health",
                                            headers={"Authorization": f"Bearer {token}"})
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError("Local MCP HTTP readiness timed out")


def _result(data, error=False):
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))], isError=error)


class Adapter:
    def __init__(self, docker, shuttle):
        self.docker, self.shuttle = docker, shuttle
        self.enabled = True
        self.local_names = set()

    async def tools(self):
        local = (await self.shuttle.list_tools()).tools
        remote = (await self.docker.list_tools()).tools
        self.local_names = {tool.name for tool in local}
        if self.local_names & {tool.name for tool in remote}:
            raise RuntimeError("Shuttle and Docker tool names collide; remove the duplicate connection")
        return [*remote, *(local if self.enabled else [])]

    async def call(self, name, arguments):
        if name in self.local_names:
            if not self.enabled:
                return _result({"error": "Agent Shuttle is disabled; use mcp-add"}, True)
            return await self.shuttle.call_tool(name, arguments)
        if name == "mcp-exec" and arguments.get("name") in self.local_names:
            return await self.call(arguments["name"], arguments.get("arguments") or {})
        if name in {"mcp-add", "mcp-remove"} and arguments.get("name") == "agent-shuttle":
            self.enabled = name == "mcp-add"
            return _result({"name": "agent-shuttle", "enabled": self.enabled, "transport": "host-stdio"})
        result = await self.docker.call_tool(name, arguments)
        if name != "mcp-find" or result.isError:
            return result
        text = next((item.text for item in result.content if item.type == "text"), "{}")
        data = json.loads(text)
        tokens = re.findall(r"[\w-]{3,}", arguments.get("query", "").casefold())
        searchable = (TITLE + " " + CATALOG_DESCRIPTION).casefold()
        if not tokens or any(token in searchable for token in tokens):
            servers = [item for item in data.get("servers", []) if item.get("name") != "agent-shuttle"]
            servers.insert(0, {"name": "agent-shuttle", "description": CATALOG_DESCRIPTION,
                               "transport": "host-stdio", "enabled": self.enabled, "long_lived": True})
            data["servers"] = servers[:arguments.get("limit", 10)]
            data["total_matches"] = max(data.get("total_matches", 0) + 1, len(servers))
        return _result(data)


async def run_gateway(profile, workspace=None):
    if is_worker_context():
        from .mcp_server import mcp
        await mcp.run_stdio_async()
        return
    executable = shutil.which("docker")
    if not executable:
        raise RuntimeError("Docker CLI is not installed")
    version = await asyncio.to_thread(subprocess.run, [executable, "mcp", "version"],
                                      capture_output=True, text=True, timeout=15)
    if version.returncode or not re.search(r"\bv?0\.44\.1\b", version.stdout):
        raise RuntimeError("This release verifies Docker MCP 0.44.1; use that version or direct MCP stdio")
    root = Path(workspace or os.environ.get("AGENT_SHUTTLE_WORKSPACE") or Path.cwd()).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace must be a directory")
    env = {k: v for k, v in os.environ.items() if k != TOKEN_ENV}
    env["AGENT_SHUTTLE_WORKSPACE"] = str(root)
    docker_params = StdioServerParameters(command=executable,
        args=["mcp", "gateway", "run", "--profile", profile, "--log-calls=false"], env=env)
    shuttle_params = StdioServerParameters(command=sys.executable,
        args=["-m", "agent_shuttle.mcp_server"], env=env)
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
        async with stdio_client(docker_params, errlog=errors) as (dr, dw), stdio_client(shuttle_params, errlog=errors) as (sr, sw):
            async with ClientSession(dr, dw) as docker, ClientSession(sr, sw) as shuttle:
                async with asyncio.timeout(30):
                    await docker.initialize()
                    initialized = await shuttle.initialize()
                adapter = Adapter(docker, shuttle)
                await adapter.tools()
                server = Server("Agent Shuttle + Docker MCP", version=VERSION, instructions=(
                    initialized.instructions or "") + " Shuttle is a host stdio backend alongside Docker. "
                    "mcp-find searches both. Invoke Shuttle tools directly or through mcp-exec; "
                    "Docker code-mode only composes Docker-owned tools.")

                @server.list_tools()
                async def tools():
                    return await adapter.tools()

                @server.call_tool()
                async def call(name, arguments):
                    result = await adapter.call(name, arguments or {})
                    if name in {"mcp-add", "mcp-remove"}:
                        await server.request_context.session.send_tool_list_changed()
                    return result

                @server.list_prompts()
                async def prompts():
                    return (await docker.list_prompts()).prompts

                @server.get_prompt()
                async def prompt(name, arguments):
                    return await docker.get_prompt(name, arguments)

                @server.list_resources()
                async def resources():
                    local = (await shuttle.list_resources()).resources if adapter.enabled else []
                    remote = (await docker.list_resources()).resources
                    if {str(x.uri) for x in local} & {str(x.uri) for x in remote}:
                        raise RuntimeError("Shuttle and Docker resource URIs collide")
                    return [*local, *remote]

                @server.list_resource_templates()
                async def templates():
                    local = (await shuttle.list_resource_templates()).resourceTemplates if adapter.enabled else []
                    remote = (await docker.list_resource_templates()).resourceTemplates
                    if {x.uriTemplate for x in local} & {x.uriTemplate for x in remote}:
                        raise RuntimeError("Shuttle and Docker resource templates collide")
                    return [*local, *remote]

                @server.read_resource()
                async def read(uri):
                    if str(uri).startswith("agent-shuttle://"):
                        if not adapter.enabled:
                            raise ValueError("Agent Shuttle is disabled")
                        result = await shuttle.read_resource(uri)
                    else:
                        result = await docker.read_resource(uri)
                    return [ReadResourceContents(content=item.text if hasattr(item, "text") else base64.b64decode(item.blob),
                                                 mime_type=item.mimeType) for item in result.contents]

                async with stdio_server() as (reader, writer):
                    await server.run(reader, writer, server.create_initialization_options())
