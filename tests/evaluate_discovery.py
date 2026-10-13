"""Explicit schema-only model-selection evaluation; never dispatches fixture tasks.

Each case is a fresh no_tools Antigravity turn in an empty temporary workspace.
Transport and actual task execution are covered separately by Docker smoke tests.
"""
import argparse
import asyncio
import json
import socket
import tempfile
from pathlib import Path

from agent_shuttle.client import ShuttleClient
from agent_shuttle.managed import HarnessLaunch, connect_harness
from agent_shuttle.mcp_server import mcp
from agent_shuttle.presentation import VERSION


async def evaluate(model, output, retry_failed=False):
    output.parent.mkdir(parents=True, exist_ok=True)
    journal = output.with_suffix(".jsonl")
    journal.write_text("", encoding="utf-8")
    cases = json.loads((Path(__file__).parent / "fixtures/discovery_queries.json").read_text(encoding="utf-8"))["cases"]
    previous = {}
    if retry_failed:
        saved = json.loads(output.read_text(encoding="utf-8"))
        if saved.get("requested_model") != model:
            raise ValueError("A transport retry must preserve the requested evaluator model")
        previous = {item["id"]: item for item in saved["results"]}
        output.with_suffix(".previous.json").write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    tool_definitions = [tool.model_dump(mode="json", exclude_none=True) for tool in await mcp.list_tools()]
    with tempfile.TemporaryDirectory(prefix="shuttle-selection-", ignore_cleanup_errors=True) as folder:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        launch = HarnessLaunch("antigravity", url, Path(folder), agy_turn_timeout_seconds=180)
        async with connect_harness(launch) as peer:
            client = ShuttleClient(timeout_seconds=210)
            # Identity reports backend-enforced policies without an upstream metadata call.
            capabilities = await client.identity(peer.url)
            if "no_tools" not in capabilities.get("supported_tool_policies", []):
                raise RuntimeError("Evaluation requires enforced no_tools; unrestricted execution is not allowed")
            try:
                info = await client.info(peer.url)
            except Exception as exc:
                info = {"capabilities": {"available": False, "error": type(exc).__name__}}
            semaphore = asyncio.Semaphore(1)
            async def one(case):
                prior = previous.get(case["id"])
                if prior and not (prior.get("error") and prior.get("state") == "TASK_STATE_FAILED"):
                    return prior
                async with semaphore:
                    prompt = (
                        "You are evaluating tool selection, not doing the user's task. "
                        "Use only the tool definitions below as context; do not execute tools. "
                        "A configured agent_id auditor is available. Decide whether the user asks to "
                        "delegate work or discover actual available local agents and delegation tools. "
                        "General advice, ordinary explanations, model recommendations and requests "
                        "to answer yourself do not request an agent action. "
                        "Return only JSON with delegation_intent (boolean), agent_id (string or null), "
                        "model (exact user-requested model string or null), and tools (array of relevant "
                        "tool names). Preserve explicit agent and model. If a display model name needs "
                        "resolution, select get_agent_info first and preserve the requested name in model. "
                        "For a negative case tools may be empty.\nTOOLS:\n" + json.dumps(tool_definitions, ensure_ascii=False) +
                        "\nUSER REQUEST:\n" + case["query"])
                    response = None
                    try:
                        response = await client.ask(peer.url, prompt, model=model, tool_policy="no_tools")
                        text = response.text.strip()
                        if text.startswith("```"):
                            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
                        decision = json.loads(text)
                        valid = decision.get("delegation_intent") is case["delegation_intent"]
                        if case["delegation_intent"]:
                            valid = valid and bool(decision.get("tools")) and all(name in {t["name"] for t in tool_definitions} for name in decision["tools"])
                            if case.get("agent_id"):
                                valid = valid and decision.get("agent_id") == case["agent_id"]
                            if case.get("model"):
                                valid = valid and decision.get("model") == case["model"]
                        else:
                            valid = valid and not set(decision.get("tools", [])) & {"ask_agent", "submit_task", "ask_codex", "ask_antigravity"}
                        result = {"id": case["id"], "positive": case["delegation_intent"], "passed": bool(valid), "decision": decision}
                    except Exception as exc:
                        result = {"id": case["id"], "positive": case["delegation_intent"], "passed": False, "error": type(exc).__name__}
                        if response is not None:
                            result.update(state=response.state, output=response.text[:2000])
                    if prior:
                        result["prior_attempt"] = prior
                    with journal.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(result, ensure_ascii=False) + "\n")
                    print(case["id"], "PASS" if result["passed"] else "FAIL", flush=True)
                    return result
            results = await asyncio.gather(*(one(case) for case in cases))
    positive = sum(item["passed"] and item["positive"] for item in results)
    negative = sum(item["passed"] and not item["positive"] for item in results)
    report = {"version": VERSION, "evaluation": "fresh-turn schema-only selection; no fixture task executed",
              "harness": "antigravity", "requested_model": model,
              "model_catalog": info.get("capabilities", {}), "policy": "no_tools",
              "positive_passed": positive, "negative_passed": negative,
              "backend_errors": sum(bool(item.get("error")) for item in results),
              "passed": positive >= 15 and negative == 8, "results": results}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="Optional explicit evaluator model; otherwise the harness default")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--retry-failed", action="store_true", help="Retry only failed backend tasks from the saved report, serially with the same model and no_tools")
    args = parser.parse_args()
    asyncio.run(evaluate(args.model, args.output, args.retry_failed))
