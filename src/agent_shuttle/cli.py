"""Command line entry points for the bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import socket
from pathlib import Path

import uvicorn

from .a2a_server import make_app
from .client import BridgeClient
from .discovery import discover_harnesses
from .profiles import AgentProfile
from .registry import build_builtin, build_profile


def _run_server(app, port: int) -> None:
    """Bind first so lifespan can safely publish the credential for this port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen(2048)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port))
        asyncio.run(server.serve(sockets=[listener]))


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-shuttle")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Expose a local agent profile through A2A")
    serve.add_argument("agent", choices=["codex", "antigravity", "profile"])
    serve.add_argument("--profile", type=Path, help="JSON profile for OpenCode, Claude Code or ACP")
    serve.add_argument("--workspace", type=Path, help="Project directory; overrides profile workspace")
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--task-db", type=Path, help="Persist tasks and request bindings in this SQLite file")
    serve.add_argument("--execution-timeout-seconds", type=float, default=1800)
    serve.add_argument("--stall-timeout-seconds", type=float, default=1800)
    serve.add_argument("--agy-command", default=os.environ.get("BRIDGE_AGY_COMMAND", "agy"))
    serve.add_argument("--agy-mode", choices=["cli", "sdk"], default="cli")
    serve.add_argument("--agy-python", type=Path)
    serve.add_argument(
        "--agy-dangerously-skip-permissions", action="store_true",
        help="Antigravity CLI only: approve every tool call for this Bridge server",
    )
    serve.add_argument(
        "--agy-turn-timeout-seconds", type=float, default=None,
        help="Antigravity CLI only: maximum one-shot turn duration (default: 300 seconds)",
    )
    ask = sub.add_parser("ask", help="Send a text task to an A2A agent")
    ask.add_argument("url")
    ask.add_argument("prompt")
    ask.add_argument("--model", help="Select the remote agent's model ID")
    ask.add_argument("--reasoning-effort", help="Select the provider-specific reasoning effort")
    ask.add_argument("--tool-policy", choices=["no_tools", "read_only", "workspace_write", "full_access"])
    info = sub.add_parser("info", help="Read live models, reasoning efforts and account quotas")
    info.add_argument("url")
    discover = sub.add_parser("discover", help="List locally installed harnesses without starting them")
    discover.add_argument("--agy-command")
    discover.add_argument("--opencode-command")
    discover.add_argument("--claude-command")
    discover.add_argument("--profile", type=Path, help="Check the configured ACP command without starting it")
    doctor = sub.add_parser("doctor", help="Check local agent readiness without a model turn")
    doctor.add_argument("agent_id", nargs="?", help="Built-in or BRIDGE_AGENTS_JSON agent ID")
    doctor.add_argument("--profile", type=Path, help="Check one JSON agent profile")
    doctor.add_argument("--smoke", action="store_true", help="Run one model turn with the safest enforced policy")
    doctor.add_argument("--json", action="store_true", help="Print a machine-readable diagnostic report")
    args = parser.parse_args()
    if args.command == "doctor":
        from .doctor import DoctorConfigError, diagnose, format_report
        try:
            report = asyncio.run(diagnose(args.agent_id, profile_path=args.profile, smoke=args.smoke))
        except DoctorConfigError as exc:
            if args.json:
                print(json.dumps({"schema_version": 1, "status": "ERROR", "checks": [],
                                  "error": str(exc)}, ensure_ascii=False))
            else:
                print(f"doctor: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_report(report))
        if report["status"] == "FAIL":
            raise SystemExit(1)
        return
    if args.command == "discover":
        overrides = {name: value for name, value in (
            ("antigravity", args.agy_command), ("opencode", args.opencode_command),
            ("claude_code", args.claude_command),
        ) if value}
        result = discover_harnesses(overrides)
        if args.profile:
            profile = AgentProfile.from_file(args.profile)
            if profile.runtime != "acp":
                parser.error("discover --profile requires an ACP profile")
            from .acp_runtime import AcpRuntime
            result[profile.id] = asyncio.run(AcpRuntime(profile).discover())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "ask":
        result = asyncio.run(
            BridgeClient().ask(
                args.url,
                args.prompt,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                tool_policy=args.tool_policy,
            )
        )
        print(f"{result.state} task={result.task_id}")
        print(result.text)
        return
    if args.command == "info":
        print(json.dumps(asyncio.run(BridgeClient().info(args.url)), ensure_ascii=False, indent=2))
        return
    if args.agy_dangerously_skip_permissions and (args.agent != "antigravity" or args.agy_mode != "cli"):
        parser.error("--agy-dangerously-skip-permissions requires serve antigravity --agy-mode cli")
    if args.agy_turn_timeout_seconds is not None and (args.agent != "antigravity" or args.agy_mode != "cli"):
        parser.error("--agy-turn-timeout-seconds requires serve antigravity --agy-mode cli")
    if args.agent == "profile":
        if args.profile is None:
            parser.error("serve profile requires --profile JSON_PATH")
        profile = AgentProfile.from_file(args.profile, workspace_override=args.workspace)
        backend, info_provider = build_profile(profile)
        name = profile.id
    else:
        if args.profile is not None:
            parser.error("--profile is only valid with serve profile")
        workspace = (args.workspace or Path.cwd()).resolve(strict=True)
        if not workspace.is_dir():
            parser.error("--workspace must be a directory")
        name = args.agent
    if args.agent != "profile":
        backend, info_provider = build_builtin(
            args.agent, workspace, command=args.agy_command, mode=args.agy_mode,
            python=args.agy_python,
            dangerously_skip_permissions=args.agy_dangerously_skip_permissions,
            turn_timeout_seconds=args.agy_turn_timeout_seconds or 300,
        )
    url = f"http://127.0.0.1:{args.port}"
    store = None
    if args.task_db is not None:
        from .task_store import SQLiteTaskStore
        store = SQLiteTaskStore(args.task_db)
    app = make_app(name, backend, url, info_provider, task_store=store,
                   execution_timeout_seconds=args.execution_timeout_seconds,
                   stall_timeout_seconds=args.stall_timeout_seconds,
                   publish_credential=True)
    _run_server(app, args.port)


if __name__ == "__main__":
    main()
