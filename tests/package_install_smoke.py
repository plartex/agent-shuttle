"""Exercise a built wheel through uv, outside the source checkout."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


MCP_PROBE = r"""
import asyncio
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def probe():
    server = StdioServerParameters(command=sys.argv[1])
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            required = {"ask_agent", "get_agent_info"}
            if not required.issubset(names):
                raise AssertionError(f"Missing MCP tools: {required - names}")

asyncio.run(asyncio.wait_for(probe(), timeout=30))
"""


def run(*args: str, cwd: Path, env: dict[str, str] | None = None,
        timeout: int = 120) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        raise AssertionError(f"{args!r} exited {result.returncode}: {result.stderr[-3000:]}")
    return result


def main() -> None:
    dist = Path(sys.argv[1]).resolve()
    wheels = list(dist.glob("agent_shuttle-*.whl"))
    sdists = list(dist.glob("agent_shuttle-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise AssertionError("Expected one Agent Shuttle wheel and one sdist")
    uv = shutil.which("uv")
    if uv is None:
        raise AssertionError("uv must be installed for the package smoke test")

    with tempfile.TemporaryDirectory(prefix="agent-shuttle-install-") as folder:
        root = Path(folder)
        env = os.environ.copy()
        env.update({"UV_TOOL_DIR": str(root / "tools"),
                    "UV_TOOL_BIN_DIR": str(root / "bin"),
                    "UV_CACHE_DIR": str(root / "cache")})
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        run(uv, "tool", "install", "--python", version, str(wheels[0]),
            cwd=root, env=env, timeout=300)

        suffix = ".exe" if os.name == "nt" else ""
        shuttle = root / "bin" / f"agent-shuttle{suffix}"
        mcp = root / "bin" / f"agent-shuttle-mcp{suffix}"
        if not shuttle.is_file() or not mcp.is_file():
            raise AssertionError("uv did not install both command entry points")
        run(str(shuttle), "--help", cwd=root, env=env)
        discovered = run(str(shuttle), "discover", cwd=root, env=env)
        if not isinstance(json.loads(discovered.stdout), dict):
            raise AssertionError("discover did not return a JSON object")

        scripts = "Scripts" if os.name == "nt" else "bin"
        tool_python = root / "tools" / "agent-shuttle" / scripts / ("python.exe" if os.name == "nt" else "python")
        if not tool_python.is_file():
            raise AssertionError("uv tool environment was not created")
        run(str(tool_python), "-c", MCP_PROBE, str(mcp), cwd=root, env=env,
            timeout=60)

        library_env = root / "library"
        run(uv, "venv", "--python", version, str(library_env), cwd=root, env=env)
        library_python = library_env / scripts / ("python.exe" if os.name == "nt" else "python")
        run(uv, "pip", "install", "--python", str(library_python), str(wheels[0]),
            cwd=root, env=env, timeout=300)
        imported = run(str(library_python), "-c",
                       "import agent_shuttle, json, sys; "
                       "print(json.dumps({'prefix': sys.prefix, 'module': agent_shuttle.__file__}))",
                       cwd=root, env=env)
        import_info = json.loads(imported.stdout)
        if not Path(import_info["prefix"]).samefile(library_env):
            raise AssertionError("Python ran outside the project environment")
        if Path(import_info["module"]).resolve().is_relative_to(Path(__file__).resolve().parents[1]):
            raise AssertionError("Python imported Agent Shuttle from the source checkout")
        print("uv tool install, both entry points, MCP handshake, and library import passed")


if __name__ == "__main__":
    main()
