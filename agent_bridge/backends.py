"""Execution adapters. Each A2A request starts a fresh agent conversation."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Protocol


class Backend(Protocol):
    async def run(self, prompt: str, model: str | None = None) -> str: ...


class CodexBackend:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve(strict=True)

    async def run(self, prompt: str, model: str | None = None) -> str:
        from openai_codex import AsyncCodex, CodexConfig, Sandbox

        # The Windows CLI needs an explicit home in some non-interactive shells.
        codex_home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        config = CodexConfig(env={**os.environ, "CODEX_HOME": codex_home})
        async with AsyncCodex(config) as codex:
            thread = await codex.thread_start(
                cwd=str(self.workspace),
                model=model,
                sandbox=Sandbox.workspace_write,
            )
            result = await thread.run(prompt)
            return result.final_response or ""


class AntigravityCliBackend:
    """Run the official `agy` headless CLI with its configured sign-in."""

    def __init__(self, workspace: Path, command: str = "agy"):
        self.workspace = workspace.resolve(strict=True)
        self.command = command

    async def run(self, prompt: str, model: str | None = None) -> str:
        command = [self.command, "-p", prompt, "--output-format", "json"]
        if model:
            command.extend(["--model", model])
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.workspace),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise RuntimeError(
                f"agy failed ({process.returncode}): {stderr.decode(errors='replace')[-4000:]}"
            )
        result = json.loads(stdout)
        if result.get("status") != "SUCCESS":
            raise RuntimeError(str(result.get("error") or result.get("status") or "agy failed"))
        return str(result.get("response", ""))


def default_agy_python() -> Path:
    root = Path(__file__).resolve().parents[2]
    folder = "Scripts" if os.name == "nt" else "bin"
    name = "python.exe" if os.name == "nt" else "python"
    return root / ".venv-agy" / folder / name


class AntigravitySdkBackend:
    """Optional Antigravity SDK adapter in a separate Protobuf environment."""

    def __init__(self, workspace: Path, python: Path | None = None):
        self.workspace = workspace.resolve(strict=True)
        self.python = (python or default_agy_python()).resolve()

    async def run(self, prompt: str, model: str | None = None) -> str:
        if not self.python.is_file():
            raise RuntimeError(f"Antigravity Python environment missing: {self.python}")
        process = await asyncio.create_subprocess_exec(
            str(self.python),
            "-m",
            "agent_bridge.agy_worker",
            cwd=str(self.workspace),
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = json.dumps({"prompt": prompt, "workspace": str(self.workspace), "model": model}).encode()
        try:
            stdout, stderr = await process.communicate(payload)
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        marker = b"AGENT_BRIDGE_RESULT="
        lines = [line[len(marker):] for line in stdout.splitlines() if line.startswith(marker)]
        if process.returncode or not lines:
            detail = stderr.decode(errors="replace")[-4000:]
            raise RuntimeError(f"Antigravity worker failed ({process.returncode}): {detail}")
        result = json.loads(lines[-1])
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Unknown Antigravity error"))
        return str(result.get("text", ""))
