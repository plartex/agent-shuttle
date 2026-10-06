# Agent Shuttle

[![Tests](https://github.com/Plartex/agent-shuttle/actions/workflows/tests.yml/badge.svg)](https://github.com/Plartex/agent-shuttle/actions/workflows/tests.yml)
[![MIT license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![A2A Protocol 1.0](https://img.shields.io/badge/A2A-1.0_JSON--RPC-blue)](https://a2a-protocol.org/latest/)
[![MCP](https://img.shields.io/badge/MCP-tools-green)](https://modelcontextprotocol.io/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)

[Russian version / Русская версия](README.ru.md)

Agent Shuttle gives Python applications one way to work with **Codex**, **Antigravity**, **OpenCode**, **Claude Code**, and configured **ACP agents**. It runs local agent tasks, preserves multi-turn sessions, and exposes the agents through [A2A 1.0 JSON-RPC](https://a2a-protocol.org/latest/) and [MCP](https://modelcontextprotocol.io/).

It allows agents and external applications to delegate tasks to peer agents, reuse multi-turn conversations, query live model catalogs and account quotas, and report each runtime's tool permission guarantees—all on local loopback (`127.0.0.1`) without sharing cloud API keys.

File editing tasks can use [isolated Git worktrees with explicit change application](docs/isolated-workspaces.md).

---

## Supported Agent Harnesses

| Harness | Primary Integration Mechanism | Auth & Model Access | Key Features |
|---|---|---|---|
| **Codex** | Official `openai-codex` Python SDK | Local Codex App Server sign-in | Sandboxes (`workspace_write`, `read_only`, `full_access`), model & reasoning effort catalog, quota reporting via `account/rateLimits/read`. |
| **Antigravity** | Official `agy` CLI in headless mode (`-p` / `stream-json`) | Signed-in Antigravity account | Real-time models, efforts, and `/usage` quotas. Default settings or full access (`--dangerously-skip-permissions`). Optional legacy SDK backend. |
| **OpenCode** | Managed local HTTP server (`--pure serve`) | Local Ollama or cloud providers | Configured via JSON profile, fine-grained tool policies (`no_tools`, `read_only`, `workspace_write`, `full_access`), model variants. |
| **Claude Code** | Managed CLI in print mode (`claude -p`) | Local Ollama endpoint or Anthropic | Configured via JSON profile, isolated temporary session configs, resume support, safe mode vs full access. |
| **Configured ACP agent** | Agent Client Protocol over local stdio | Agent's own account and model configuration | Register a command in a JSON profile; streamed updates, cancellation, and capability-gated session loading. Tool policies are advisory. |

---

## Installation

Agent Shuttle runs on Windows, Linux, and macOS. For the command-line tools and MCP server, install [uv](https://docs.astral.sh/uv/getting-started/installation/) first; uv can obtain a compatible Python automatically.

### Install the commands and MCP server

```text
uv tool install agent-shuttle
uv tool dir --bin
```

The last command shows where uv placed `agent-shuttle-mcp` (with `.exe` on Windows); use its absolute path in your MCP client's configuration. To use `agent-shuttle` directly in a shell, run `uv tool update-shell` if the command is not on `PATH`, then open a new shell.

To check local harness availability, run `agent-shuttle discover`; it does not authenticate or send a model request. `agent-shuttle doctor <agent> --smoke` checks a real model turn when needed. MCP already provides `ask_agent` for supported harnesses and configured profiles.

### Use the Python library in another project

```text
uv add agent-shuttle
```

`uv tool install` isolates the tool from your project's Python environment. Install Agent Shuttle as a project dependency when your code imports `agent_shuttle`.

### Install from Local Checkout

You can install Agent Shuttle directly from its repository checkout into your project's virtual environment:

```powershell
# Create and activate your virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install in standard or editable mode
pip install C:\path\to\agent-shuttle
# or editable mode during development:
# pip install -e C:\path\to\agent-shuttle
```

Once installed, the CLI tools (`agent-shuttle`, `agent-shuttle-mcp`) and Python API (`agent_shuttle`) are fully accessible inside that virtual environment. The original checkout directory does not need to stay in place for runtime imports. Version 0.6 removes the old `agent_bridge` Python imports and `agent-bridge` commands; the `agent_bridge.*` A2A metadata keys remain part of the wire protocol.

The distribution has one Python package: `src/agent_shuttle/`. This is the standard `src` layout; there is no second implementation or compatibility package.

---

## Quickstart

Install the MCP server, then give your MCP client its absolute executable path:

```text
uv tool install agent-shuttle
uv tool dir --bin
```

Point your MCP client's `command` at `agent-shuttle-mcp` in the printed directory. MCP starts a local A2A peer when a request needs one; there is no separate server startup step. See the [getting started guide](docs/getting-started.md) for MCP configuration and harness setup.

For a persistent server, run `agent-shuttle serve codex --workspace . --port 8765` (or `serve antigravity` on port 8766) in a terminal and stop it with Ctrl+C.

---

## Minimal Examples

### 1. Python API

```python
import asyncio
from pathlib import Path
from agent_shuttle import ShuttleClient, HarnessLaunch, connect_harness

async def main():
    client = ShuttleClient()

    # Query live model catalog and quota information
    info = await client.info("http://127.0.0.1:8766")
    print("Selected model:", info["capabilities"]["selected_model"])

    # One-shot task
    result = await client.ask(
        "http://127.0.0.1:8766",
        "Explain the project structure and list entry points.",
        model="gemini-3.8-flash-medium",
        reasoning_effort="medium",
    )
    print(f"[{result.state}] Task {result.task_id}:\n{result.text}")

    # Persistent multi-turn session (preserves conversation context)
    async with client.session(
        "http://127.0.0.1:8765",
        model="gpt-5.6-terra",
        reasoning_effort="high",
    ) as session:
        step1 = await session.ask("What database migrations are pending?")
        step2 = await session.ask("Generate SQL to apply the first migration.")
        print("Step 2 response:", step2.text)
        print("Step 2 token usage:", step2.usage)

    # Managed peer lifecycle: reuse existing server or start a temporary one
    launch = HarnessLaunch(
        name="antigravity",
        url="http://127.0.0.1:8766",
        workspace=Path.cwd(),
    )
    async with connect_harness(launch) as conn:
        print("Connected to:", conn.url, "(spawned temporary:", conn.started, ")")

asyncio.run(main())
```

### 2. Command-Line Interface (CLI)

```powershell
# Discover locally installed harnesses without starting models
agent-shuttle discover

# Check installation and connection without a model turn
agent-shuttle doctor
agent-shuttle doctor --profile .\examples\opencode-ollama.json

# After fixing a reported issue, verify one real turn (uses model tokens)
agent-shuttle doctor codex --smoke

# Start an A2A server for Codex
agent-shuttle serve codex --workspace . --port 8765

# Start an A2A server for Antigravity (CLI mode)
agent-shuttle serve antigravity --workspace . --port 8766

# Start an A2A server from a profile (OpenCode, Claude Code or ACP)
agent-shuttle serve profile --profile .\examples\opencode-ollama.json --workspace . --port 8767

# Query server capabilities, models, and quota limits
agent-shuttle info http://127.0.0.1:8765

# Send a task from the command line
agent-shuttle ask http://127.0.0.1:8765 "Summarize recent changes" --model gpt-5.6-terra
```

`doctor` reports installation, connection and turn verification separately. Its default run checks built-ins and local profiles registered in `BRIDGE_AGENTS_JSON`; missing optional runtimes are skipped. OpenCode and Claude Code need a JSON profile for connection checks. `--json` provides the same report for scripts. Exit codes are `0` for passed checks, `1` for a failed target or smoke turn, and `2` for invalid invocation or configuration. `--smoke` requires one target and uses enforced `no_tools` (or Codex `read_only`); ACP smoke is rejected because ACP cannot enforce those restrictions.

### Register an ACP agent

Copy [`examples/acp-worker.json`](examples/acp-worker.json), set `command` to the installed agent's ACP launch command, and set `workspace` to the project directory. `command` is an argument array; it is never run through a shell. `provider`, `default_model`, `allowed_models`, and `reasoning_efforts` are optional for ACP. Without allowlists, an explicit model or effort may select any variant advertised in the session's ACP config options; configured allowlists narrow those choices.

```powershell
agent-shuttle discover --profile .\examples\acp-worker.json
agent-shuttle serve profile --profile .\examples\acp-worker.json --port 8768
agent-shuttle info http://127.0.0.1:8768
agent-shuttle ask http://127.0.0.1:8768 "Explain this project" --tool-policy read_only
```

For MCP managed startup, map an arbitrary ID to the profile in `BRIDGE_AGENTS_JSON`:

```json
{"my-acp": {"harness": "acp", "profile": "C:/profiles/my-acp.json"}}
```

ACP discovery checks the command without starting the agent. `info` performs the ACP handshake and reports negotiated capabilities without sending a model prompt. ACP policies are **advisory**: results include a warning, and `read_only_tools` remains false because the agent may use tools outside client permission requests. Use a verified external sandbox when an enforceable restriction is required.

### 3. Model Context Protocol (MCP)

Start the stdio MCP server:
```powershell
agent-shuttle-mcp
# or: python -m agent_shuttle.mcp_server
```

Available MCP tools:
- `ask_agent(agent_id, prompt, model?, reasoning_effort?, tool_policy?, workspace?)`: Reuses a matching local A2A server or starts a temporary one. Built-in IDs are `codex`, `antigravity`, `opencode`, and `claude_code`; configured ACP IDs require a JSON profile in `BRIDGE_AGENTS_JSON`.
- `get_agent_info(agent_id, workspace?)`: Fetches live models and quotas, starting a temporary server if needed.
- `ask_antigravity(prompt, model?, reasoning_effort?, workspace?, tool_policy?, turn_timeout_seconds=300)`: Starts an Antigravity server if one is not running.
- `ask_codex(prompt, model?, reasoning_effort?, workspace?)`: Starts a Codex server if one is not running.
- `get_antigravity_info(workspace?)` & `get_codex_info()`: Read live capabilities and quota without burning model turns.
- `submit_task(agent_id, prompt, model?, reasoning_effort?, tool_policy?, workspace?, request_id?)`: Start a long task and return its ID immediately.
- `check_task(task_id)`, `wait_task(task_id, timeout_seconds?)`, `cancel_task(task_id)`: Inspect, wait for, or stop a task.
- `get_result(task_id, cursor?, limit?)`, `get_transcript(task_id, cursor?, limit?)`: Read bounded pages of output and history.

---

## Key Concepts

### Harness Discovery
Run `agent-shuttle discover` (or `discover_harnesses()` in Python) to inspect local executables without launching processes or loading weights. It checks `PATH` and platform-specific standard installation directories (`%LOCALAPPDATA%\agy\bin`, npm global directories, etc.). Custom paths can be specified via environment variables (`BRIDGE_AGY_COMMAND`) or CLI flags (`--agy-command`, `--opencode-command`, `--claude-command`).

### Model & Reasoning Selection
Model parameters are passed as A2A metadata keys (`agent_bridge.model`, `agent_bridge.reasoning_effort`):
- **Antigravity:** Reasoning effort is embedded in model IDs (e.g. `gemini-3.8-flash-medium`). If both `--model` and `--effort` are passed, they must match.
- **Codex:** Model and reasoning effort are configured independently according to the catalog returned by `get_codex_info`.
- **OpenCode & Claude Code:** Profiles define `allowed_models` and optional `reasoning_efforts` (such as model variants for Ollama or CLI flags).

### Workspaces & Session Isolation
- Every server binds to a strictly validated, canonical workspace directory.
- `connect_harness()` verifies that an existing server's workspace matches the caller's target workspace before reusing it.
- **Sessions:** `BridgeSession` maintains a stateful conversation across multiple `ask()` calls. Conversation settings (model, effort, tool policy) are pinned at session creation and cannot be changed mid-session. Idle sessions are cleaned up automatically after 30 minutes.
- **Library task lifecycle:** `TaskManager` runs agents directly from Python, with no A2A or MCP server. It owns task IDs, sessions, a SQLite event journal, cancellation, result paging, preferences, and interruption recovery. A2A projects the same task ID and result through its protocol; MCP continues to reach those tasks through managed A2A peers. See the [Python API](docs/api.md#taskmanager-library-api).
- **Remote task lifecycle:** `ShuttleClient.submit()` returns a remote `TaskHandle` immediately. Use `status()`, bounded `wait(timeout)`, `events()`, `result_page()`, `transcript()`, `result()`, or `cancel()`; reopen a task by ID with `client.task(url, task_id)`. A wait timeout does not stop the agent. An optional UUID `request_id` deduplicates retried submissions. Standalone servers can persist tasks with `--task-db`; MCP task tools do this automatically in the workspace's `.agent-shuttle` directory. Completed results survive restart; interrupted work is marked failed without replay. See the [API reference](docs/api.md#taskhandle-and-bridgeevent).

### Safety & Tool Policies
Agent Shuttle defines four standardized tool policies:
- `no_tools`: Disables tool invocations entirely.
- `read_only`: Permits non-mutating search and file reading.
- `workspace_write`: Allows editing files within the designated workspace.
- `full_access`: Explicitly unclamps all tool restrictions and approval prompts.

For configured ACP agents, these policies are advisory. Agent Shuttle checks ACP permission requests but cannot prevent an agent from acting outside them. See the [permissions guide](docs/permissions.md#5-configured-acp-agent).

> [!WARNING]
> `full_access` grants the worker unrestricted tool access for that task. `--agy-dangerously-skip-permissions` enables that capability for the Antigravity server. Keep servers on loopback. Antigravity's explicit scoped `read_only` policy is enforced by its verified `PreToolUse` hook; an implicit/default CLI policy does not provide the same guarantee.

---

## Testing

Agent Shuttle provides a comprehensive offline test suite using fake backends that execute without network access, credentials, or model quota consumption:

GitHub Actions runs this same suite on Windows, Ubuntu Linux, and Apple Silicon macOS (`macos-15`), each with Python 3.11 and 3.12. The process smoke uses only a local Python worker and checks process-tree cleanup; no agent CLI or account is needed.

```powershell
python -m unittest discover -s tests -v
```

Live integration tests against real models are separate, opt-in checks and are not part of the six CI jobs. They can be executed by specifying target environments (e.g. `BRIDGE_LIVE_OLLAMA_MODEL=qwen3.5:9b` or `BRIDGE_LIVE_AGY_FULL_ACCESS=1`). See [CONTRIBUTING.md](CONTRIBUTING.md) for full instructions.

---

## Documentation Index

- [Getting Started Guide](docs/getting-started.md)
- [API Reference](docs/api.md)
- [Permissions & Safety Guide](docs/permissions.md)
- [Architecture & Protocol Design](docs/architecture.md)
- [Troubleshooting & Diagnostics](docs/troubleshooting.md)
- [Contributing Guidelines](CONTRIBUTING.md)
- [Security Policy](SECURITY.md)
