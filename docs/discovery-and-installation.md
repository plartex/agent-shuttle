# Discover and connect Agent Shuttle

Agent Shuttle is both a **Python library** and an **MCP server** for delegating research, reviews, implementation and second opinions to local Antigravity, Codex, Claude Code, OpenCode and configured ACP agents. A2A is the supported peer protocol. Agent authentication stays with the installed harness. Web access depends on its available tools and enforceable policy.

## Python library

Install `uv add agent-shuttle` in a Python project and use `TaskManager`, `ShuttleClient` or `connect_harness`. The library works without an MCP client. See the [API reference](api.md).

## MCP server

Install `uv tool install agent-shuttle`. Both `agent-shuttle mcp` and `agent-shuttle-mcp` start the same stdio server. Installing the package does not connect a client.

Generate a connection preview, then apply the same command with `--apply`:

```text
agent-shuttle mcp setup --format codex-toml --config PATH_TO_CONFIG_TOML --workspace ABSOLUTE_PROJECT_PATH
agent-shuttle mcp setup --format codex-toml --config PATH_TO_CONFIG_TOML --workspace ABSOLUTE_PROJECT_PATH --apply
agent-shuttle mcp doctor --format codex-toml --config PATH_TO_CONFIG_TOML --json
```

For clients using `mcpServers` JSON, choose `--format mcp-json`. Use the client's actual configuration path. The setup command preserves unrelated entries, backs up an existing file, and rolls back its changes if the handshake fails. Existing comments in TOML are retained. Repeating an unchanged setup does not create another backup. Restart/reload the client after configuration changes.

`mcp doctor` verifies the package-to-MCP connection and reports the responding version and tools. It does not verify account authentication or run a model task. `list_agents` discovers agent IDs offline; `get_agent_info` checks live model catalogs, quota and tool policies. `agent-shuttle doctor AGENT_ID` diagnoses the selected harness; `--smoke` explicitly runs a model turn.

## Docker Gateway

Agent Shuttle 0.8 verifies Docker MCP **0.44.1**, running on the host. Configure the client's MCP server command as `agent-shuttle` with arguments `mcp gateway --profile default --workspace ABSOLUTE_PROJECT_PATH`, using the installed executable's absolute path.

The wrapper composes two local stdio backends: the actual Docker Gateway using your selected profile, and Agent Shuttle. It combines tool discovery and adds Shuttle's descriptor to `mcp-find` results. Shuttle is a host backend alongside Docker, with its own enforceable harness policies; it is not a Docker-isolated server. Docker-owned tool calls continue through the original Gateway and its policies. The profile and Docker security settings are unchanged. EOF closes the owned stdio children. No agent account credentials are copied into Docker.

Docker MCP 0.44.1 rejects loopback and non-HTTPS remote URLs by default, so its remote-server transport cannot connect to the local HTTP endpoint under standard settings. The stdio adapter avoids requiring a network-security opt-out. Shuttle tools can be called directly or using `mcp-exec`; Docker `code-mode` composes Docker-owned tools only. `mcp-add`/`mcp-remove` can enable/disable Shuttle for the adapter session, and tool-name collisions are rejected.

Search using `Antigravity`, `Codex`, `delegate`, `research`, `review`, `делегирование` or `исследование`, then use `list_agents`, `get_agent_info`, and `ask_agent`/`submit_task`. A client must use this wrapper to load the catalog; installing the Python package alone cannot change another gateway's search results.

An inherited worker uses a local stdio MCP instance with the existing nested-dispatch restriction. No coordinator service is shared with a worker, and transport tokens are removed before harness processes are launched.

For a manually managed HTTP connection:

```text
agent-shuttle-mcp --transport streamable-http --port 8769
```

Provide a randomly generated `AGENT_SHUTTLE_MCP_TOKEN` of at least 32 characters in the process environment and send `Authorization: Bearer TOKEN` to `http://127.0.0.1:8769/mcp`. The server accepts only loopback Host and rejects browser Origin headers. Do not put transport secrets in committed configuration. This direct-client HTTP mode is independent of the Docker stdio adapter.

## Public MCP Registry release

`server.json` describes `io.github.plartex/agent-shuttle`, a versioned PyPI package using stdio. Its resolved launch is `uvx agent-shuttle@VERSION mcp`. The repository README contains the matching ownership marker. Package and manifest versions must match.

1. Run offline tests, distribution validation and clean-install smoke on Windows, Linux and macOS.
2. Validate `server.json` against its pinned official JSON Schema, including the 100-character description limit.
3. Obtain publication approval and publish the verified PyPI release through the existing release workflow.
4. Authenticate the namespace using the official MCP publisher and publish the validated manifest. Read back the registry entry and verify package resolution and MCP handshake from a clean directory.
5. Repeat discovery in a fresh client chat with only the installed integration available. Record the client and model versions and results.

Preparing a manifest does not publish it. Public Registry availability and Docker catalog search are separate checks; a client's configured catalog determines which entries it can discover. Submission to Docker's public catalog requires its separate review and publication process.

## Acceptance evidence

Use the RU/EN query corpus under `tests/fixtures/discovery_queries.json`. Record actual catalog results separately from model-selection results. Static keyword checks do not prove semantic selection. Positive cases must preserve the requested agent and model; negative cases must not dispatch a task. Run `python tests/evaluate_discovery.py --output REPORT.json` explicitly to evaluate each case in a fresh Antigravity `no_tools` turn using only the advertised schemas. This consumes quota and evaluates schema-based selection, not an autonomous client conversation. Actual execution is separately covered by the Docker test harness.
