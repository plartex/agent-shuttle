"""Command line entry points for the bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import uvicorn

from .a2a_server import make_app
from .backends import AntigravityCliBackend, AntigravitySdkBackend, CodexBackend
from .client import BridgeClient
from .info import AntigravityCliInfo, AntigravitySdkInfo, CodexInfo


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Expose Codex or Antigravity as a local A2A agent")
    serve.add_argument("agent", choices=["codex", "antigravity"])
    serve.add_argument("--workspace", type=Path, default=Path.cwd())
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--agy-command", default=os.environ.get("BRIDGE_AGY_COMMAND", "agy"))
    serve.add_argument("--agy-mode", choices=["cli", "sdk"], default="cli")
    serve.add_argument("--agy-python", type=Path)
    ask = sub.add_parser("ask", help="Send a text task to an A2A agent")
    ask.add_argument("url")
    ask.add_argument("prompt")
    ask.add_argument("--model", help="Select the remote agent's model ID")
    ask.add_argument("--reasoning-effort", help="Select the provider-specific reasoning effort")
    info = sub.add_parser("info", help="Read live models, reasoning efforts and account quotas")
    info.add_argument("url")
    args = parser.parse_args()
    if args.command == "ask":
        result = asyncio.run(
            BridgeClient().ask(
                args.url,
                args.prompt,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
            )
        )
        print(f"{result.state} task={result.task_id}")
        print(result.text)
        return
    if args.command == "info":
        print(json.dumps(asyncio.run(BridgeClient().info(args.url)), ensure_ascii=False, indent=2))
        return
    workspace = args.workspace.resolve(strict=True)
    if not workspace.is_dir():
        parser.error("--workspace must be a directory")
    if args.agent == "codex":
        backend = CodexBackend(workspace)
        info_provider = CodexInfo(workspace)
    elif args.agy_mode == "sdk":
        backend = AntigravitySdkBackend(workspace, args.agy_python)
        info_provider = AntigravitySdkInfo()
    else:
        backend = AntigravityCliBackend(workspace, args.agy_command)
        info_provider = AntigravityCliInfo(workspace, args.agy_command)
    url = f"http://127.0.0.1:{args.port}"
    uvicorn.run(make_app(args.agent, backend, url, info_provider), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
