"""Real stdio MCP -> Antigravity native web-tool integration smoke."""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    with tempfile.TemporaryDirectory(prefix="shuttle-web-research-") as folder:
        env = os.environ.copy()
        env["AGENT_SHUTTLE_WORKSPACE"] = folder
        env.pop("AGENT_SHUTTLE_ANTIGRAVITY_URL", None)
        params = StdioServerParameters(command=sys.executable, args=["-m", "agent_shuttle", "mcp"], env=env)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("ask_agent", {
                    "agent_id": "antigravity", "workspace": folder, "tool_policy": "web_research",
                    "prompt": "Integration check of native web research. Use search_web to find the official Model Context Protocol documentation. Then use read_url_content to read https://modelcontextprotocol.io . Return a brief description and the source URL. Do not use local file tools, shell, MCP or subagents. Do not claim tool calls that were not performed.",
                }, read_timeout_seconds=timedelta(seconds=360))
                document = result.structuredContent
                if document is None:
                    document = json.loads(next(c.text for c in result.content if c.type == "text"))
                if len(sys.argv) > 1:
                    Path(sys.argv[1]).write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
                print(json.dumps(document, ensure_ascii=True), flush=True)
                assert not result.isError
                assert document["state"] == "TASK_STATE_COMPLETED", document
                details = document["details"]
                assert details["tool_policy"] == "web_research", details
                allowed = {item["tool"] for item in details["tool_policy_audit"] if item["decision"] == "allow"}
                assert {"search_web", "read_url_content"}.issubset(allowed), allowed
                assert all(item["decision"] == "allow" for item in details["tool_policy_audit"])
                print("PASS: MCP dispatched real web search and page reading without full_access", flush=True)

asyncio.run(main())
