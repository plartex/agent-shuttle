"""MCP launch, explicit setup, diagnostics and Docker integration."""

import argparse
import asyncio
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-shuttle mcp")
    sub = parser.add_subparsers(dest="action")
    for action in ("setup", "doctor"):
        command = sub.add_parser(action)
        command.add_argument("--format", required=True, choices=["codex-toml", "mcp-json"])
        command.add_argument("--config", type=Path, required=True)
        if action == "setup":
            command.add_argument("--workspace", type=Path, required=True)
            command.add_argument("--apply", action="store_true")
        else:
            command.add_argument("--json", action="store_true")
    gateway = sub.add_parser("gateway")
    gateway.add_argument("--profile", required=True)
    gateway.add_argument("--workspace", type=Path)
    args = parser.parse_args(argv)
    if args.action is None:
        from .mcp_server import main as server
        server([])
        return
    try:
        if args.action == "gateway":
            from .mcp_gateway import run_gateway
            asyncio.run(run_gateway(args.profile, args.workspace))
            return
        from .mcp_setup import setup, diagnose_mcp
        if args.action == "setup":
            result = asyncio.run(setup(args.config, args.format, args.workspace, args.apply))
        else:
            result = asyncio.run(diagnose_mcp(args.config, args.format))
        if args.action == "doctor" and not args.json:
            print(f"{result['status']}: MCP {result['connection']}; harness readiness {result['harness_readiness']}")
            if "error" in result:
                print(result["error"])
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") == "FAIL":
            raise SystemExit(1)
    except (OSError, ValueError, RuntimeError, TimeoutError, ExceptionGroup) as exc:
        # External configuration and subprocess errors may contain credentials.
        detail = str(exc) if args.action == "gateway" and type(exc) is RuntimeError else "check configuration and installed version"
        parser.exit(1, f"MCP {args.action} failed ({type(exc).__name__}): {detail}.\n")
