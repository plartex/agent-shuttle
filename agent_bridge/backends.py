"""Execution adapters. Each A2A request starts a fresh agent conversation."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


def _decode_agy_result(stdout: bytes, stderr: bytes) -> str:
    """Validate headless CLI output, including its exit-zero soft-denial case."""
    try:
        result = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        detail = stderr.decode(errors="replace")[-4000:].strip()
        raise RuntimeError(f"agy returned invalid JSON: {detail or exc}") from exc
    if result.get("status") != "SUCCESS":
        raise RuntimeError(str(result.get("error") or result.get("status") or "agy failed"))
    response = result.get("response")
    if not isinstance(response, str) or not response.strip():
        detail = stderr.decode(errors="replace")[-4000:].strip()
        message = (
            "agy reported SUCCESS but returned an empty response; in headless mode this "
            "usually means a requested tool was soft-denied by the permission policy"
        )
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message)
    return response


def _decode_agy_usage(stdout: bytes) -> dict[str, int]:
    raw_usage = json.loads(stdout).get("usage") or {}
    if not isinstance(raw_usage, dict):
        return {}
    return {
        key: value
        for key, value in raw_usage.items()
        if key in {"input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"}
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }


class Backend(Protocol):
    async def run(
        self,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
    ) -> str | "BackendResponse": ...


@dataclass(frozen=True)
class BackendResponse:
    text: str
    usage: dict[str, int]


class CodexBackend:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve(strict=True)

    async def run(
        self,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
    ) -> str:
        from openai_codex import AsyncCodex, CodexConfig, Sandbox

        # The Windows CLI needs an explicit home in some non-interactive shells.
        codex_home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        config = CodexConfig(env={**os.environ, "CODEX_HOME": codex_home})
        async with AsyncCodex(config) as codex:
            thread = await codex.thread_start(
                cwd=str(self.workspace),
                model=model,
                config=(
                    {"model_reasoning_effort": reasoning_effort}
                    if reasoning_effort is not None
                    else None
                ),
                sandbox=Sandbox.read_only if read_only else Sandbox.workspace_write,
            )
            result = await thread.run(prompt)
            return result.final_response or ""


class AntigravityCliBackend:
    """Run the official `agy` headless CLI with its configured sign-in."""

    def __init__(self, workspace: Path, command: str = "agy"):
        self.workspace = workspace.resolve(strict=True)
        self.command = command

    async def run(
        self,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
    ) -> BackendResponse:
        command = [self.command, "-p", prompt, "--output-format", "json"]
        if model:
            command.extend(["--model", model])
        if reasoning_effort:
            command.extend(["--effort", reasoning_effort])
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
        response = _decode_agy_result(stdout, stderr)
        return BackendResponse(response, _decode_agy_usage(stdout))


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

    async def run(
        self,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
    ) -> str:
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
        if reasoning_effort is not None:
            raise RuntimeError(
                "Antigravity SDK mode does not expose reasoning effort; use the default CLI mode"
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
