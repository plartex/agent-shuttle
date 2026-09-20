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
from .evaluation import EvaluationService, EvaluationTarget, format_text_report, load_code_smells_profile
from .evaluation.providers import agent_bridge_provider_from_name
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
    evaluate = sub.add_parser("evaluate", help="Run an LLM-first quality evaluation")
    evaluate.add_argument("target_type", choices=["snippet", "file", "project"])
    evaluate.add_argument("target", nargs="?", help="File/project path, or snippet when --code is omitted")
    evaluate.add_argument("--code", help="Code content for a snippet target")
    evaluate.add_argument("--language", help="Optional language hint for a snippet")
    evaluate.add_argument("--profile", default="code_smells", choices=["code_smells"])
    evaluate.add_argument("--rules", help="Comma-separated rule IDs; defaults to every profile rule")
    evaluate.add_argument("--batch-size", type=int, default=10)
    evaluate.add_argument("--provider", default="agent-bridge:codex")
    evaluate.add_argument("--model")
    evaluate.add_argument("--reasoning-effort")
    evaluate.add_argument("--catalog", type=Path, help="Override the bundled code-smells catalog")
    evaluate.add_argument("--json", action="store_true", dest="json_output")
    evaluate.add_argument("--output", type=Path, help="Write the selected report format to a file")
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
    if args.command == "evaluate":
        if args.target_type == "snippet":
            content = args.code if args.code is not None else args.target
            if content is None:
                parser.error("snippet requires --code or a positional target")
            target = EvaluationTarget.snippet(content, language=args.language)
        else:
            if args.code is not None:
                parser.error("--code is only valid for snippet targets")
            if args.target is None:
                parser.error(f"{args.target_type} requires a path")
            target = (
                EvaluationTarget.file(args.target)
                if args.target_type == "file"
                else EvaluationTarget.project(args.target)
            )
        rule_ids = None
        if args.rules:
            rule_ids = [item.strip() for item in args.rules.split(",") if item.strip()]
        profile = load_code_smells_profile(args.catalog, rule_ids=rule_ids)
        provider = agent_bridge_provider_from_name(args.provider)
        report = asyncio.run(
            EvaluationService(provider).evaluate(
                target,
                profile,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                batch_size=args.batch_size,
            )
        )
        rendered = report.to_json() if args.json_output else format_text_report(report)
        if args.output:
            args.output.write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
        if report.summary.errors:
            raise SystemExit(2)
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
