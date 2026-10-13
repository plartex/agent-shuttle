# Upgrading Agent Shuttle

Agent Shuttle 0.8 uses `agent_shuttle`, `AGENT_SHUTTLE_*`, and `/shuttle/*` throughout the Python library, MCP server and A2A metadata. Only the canonical package, commands and metadata namespace are supported.

Install matching Shuttle and Qriterra releases, finish active tasks, update MCP launch paths and restart connections after upgrading. Existing task databases already using the canonical namespace remain supported. Historical compatibility code is not part of this release.

See [connection setup, diagnostics and publication guidance](discovery-and-installation.md). Installation alone does not connect an MCP client. `agent-shuttle mcp setup` previews changes; `--apply` backs up and verifies a connection. `agent-shuttle mcp doctor` reports the responding server version and tools without a model turn.
