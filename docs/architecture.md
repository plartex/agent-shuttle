# Architecture and Protocol Design

[Russian version / Русская версия](ru/architecture.md)

Agent Shuttle is designed as a modular, local-first interoperability layer that connects autonomous coding harnesses without passing credentials across network boundaries. This document outlines the architectural design, protocol choices, lifecycle supervision, and session management.

## Package Layout

`src/agent_shuttle/` is the only Python package. It owns the CLI, MCP, A2A, task, and backend modules. The former `agent_bridge` import package and commands were removed in version 0.6. The `agent_bridge.*` A2A metadata keys remain part of the wire contract.

---

## High-Level Topology

```
+─────────────────────────────────────────────────────────────+
|                     Client Application                      |
|       (Python API / BridgeClient / External Caller)         |
+──────────────────────────────┬──────────────────────────────+
                               | A2A 1.0 JSON-RPC (HTTP)
                               v
+─────────────────────────────────────────────────────────────+
|               Agent Shuttle A2A Server (Starlette)           |
|  - A2A JSON-RPC Protocol Routes (/)                         |
|  - Agent Card Endpoint (/.well-known/agent-card.json)       |
|  - Read-Only Inspection Endpoints (/bridge/*)               |
|  - A2A task projection and library TaskManager              |
+──────────────┬───────────────┬───────────────┬──────────────+
               |               |               |              |
               v               v               v              v
         +-----------+   +-----------+   +-----------+  +-----------+
         |   Codex   |   |Antigravity|   | OpenCode  |  |Claude Code|
         |  Backend  |   |CLI Backend|   |  Runtime  |  |  Runtime  |
         +-----+-----+   +-----+-----+   +-----+-----+  +-----+-----+
               |               |               |              |
         +-----v-----+   +-----v-----+   +-----v-----+  +-----v-----+
         |   Codex   |   |    agy    |   | opencode  |  |  claude   |
         |App Server |   |  Headless |   |   serve   |  |   print   |
         +-----------+   +-----------+   +-----------+  +-----------+
```

When agents invoke tools on each other, they use Model Context Protocol (MCP) bridges configured via stdio:

```
Codex ──── MCP ask_antigravity ─── A2A (HTTP 127.0.0.1) ─── agy CLI ─── Antigravity
Antigravity ── MCP ask_codex ──── A2A (HTTP 127.0.0.1) ─── Codex SDK ─── Codex
```

---

## Architectural Principles

1. **Local Loopback Only (`127.0.0.1`)**:  
   Servers bind exclusively to the local loopback interface. No endpoints are exposed to the local network or internet by default.
2. **No Credential Crossing**:  
   Authentication is managed locally by each individual harness (e.g. Codex App Server token, Google Antigravity account, local Ollama instance). Bridge never accepts or transmits LLM API keys over the A2A boundary.
3. **Stateless Default with Opt-In Sessions**:  
   Each standalone `ask()` call runs in an isolated, clean task context. Multi-turn workflows use explicit `BridgeSession` handles that pin parameters and manage stateful context on the backend.
4. **Fast Readiness Verification**:  
   The `/bridge/identity` endpoint allows clients and supervisors (`connect_harness`) to verify server identity, canonical workspace, and permission mode in milliseconds without launching costly child processes or querying model APIs.

---

## Subsystem Details

### 1. Codex Backend (`agent_shuttle.backends.CodexBackend`)
- Interacts with the local Codex App Server via the official `openai-codex` Python SDK (`AsyncCodex`).
- Spawns threads bound to the canonical project directory.
- Configures thread-level reasoning effort (`model_reasoning_effort`).
- Sandboxing: Maps `tool_policy` to native `Sandbox` choices (`workspace_write`, `read_only`, `full_access`). Rejects `no_tools` as unsupported.
- Quota: Reads live rate-limit buckets from `account/rateLimits/read`.

### 2. Antigravity CLI Backend (`agent_shuttle.backends.AntigravityCliBackend`)
- Runs the official `agy` executable in headless mode:
  - One-shot turns: `agy -p <prompt> --output-format json --print-timeout <N>s`.
  - Persistent sessions: `agy --input-format stream-json --output-format stream-json --print-timeout 30m`.
- Preflight TLS retry: Automatically recovers from known preflight profile-picture handshake timeouts (up to 3 attempts).
- Soft-denial detection: Identifies when `agy` reports `SUCCESS` with denied actions or empty response, raising `AntigravityPermissionDenied`.
- Sequential metadata locking: Ensures concurrent HTTP queries to `/bridge/info` or `/bridge/capabilities` run sequentially to prevent CLI process collisions.

### 3. OpenCode Runtime (`agent_shuttle.opencode_runtime.OpenCodeRuntime`)
- Supervised HTTP daemon: Spawns `opencode --pure serve --hostname 127.0.0.1 --port <port>` on a dynamic loopback port with an autogenerated 32-byte URL-safe password.
- Inline permissions: Injects strict JSON security policies directly via `OPENCODE_CONFIG_CONTENT`.
- Environment scrubbing: Strips unrelated cloud credentials from child process environments, passing only essential OS variables and configured `credential_env`.
- Ollama integration: Configures `@ai-sdk/openai-compatible` providers with model variants representing reasoning effort levels.

### 4. Claude Code Runtime (`agent_shuttle.claude_runtime.ClaudeCodeRuntime`)
- Print-mode child processes: Invokes `claude -p --output-format json --bare --strict-mcp-config ...`.
- Isolated session storage: Creates an isolated temporary directory for `CLAUDE_CONFIG_DIR` per session.
- Conversation resumption: Starts sessions with `--session-id <UUID>` and resumes subsequent turns with `--resume <UUID>`.
- Redaction: Sanitizes stderr logs to ensure provider credentials are redacted before propagating error messages.

---

## Fixed Workspaces & Canonical Path Resolution

Working directories are strictly verified:
- Paths are resolved to their absolute, canonical representation (`path.resolve(strict=True)`).
- On Windows, paths are compared with case normalization (`os.path.normcase`).
- **Connection Guard:** `connect_harness()` inspects the reported `/bridge/identity` workspace of any running server. If the existing server's workspace does not match the requested directory, Agent Shuttle refuses to reuse the server and raises a `ValueError`. This prevents accidental cross-project modifications.

---

## Library-Owned Tasks and Sessions (`TaskManager`)

`src/agent_shuttle/task_library.py` provides the Python facade without a server. `LibraryTaskRepository` owns SQLite schema and transactions, `EventStreamService` replays the journal and delivers live events, and `WorkerLifecycleService` runs workers and native sessions. Task state changes and their events commit in one transaction. The A2A executor delegates to the facade and publishes the same task ID and outcome. The A2A task store remains a separate protocol projection; the MCP registry stores peer tickets for reconnection.

Stateful conversations are governed by `TaskManager`:

- **Turn Serialization:** Each session has its own `asyncio.Lock`. Concurrent requests targeting the same `session_id` are serialized, guaranteeing that only one turn runs at a time.
- **Pinned Settings:** The combination of `(model, reasoning_effort, tool_policy)` is frozen at session creation. Any attempt to modify these parameters mid-session is rejected.
- **Idle Reaping:** A background janitor task runs every 60 seconds within Starlette's application lifespan. Sessions that remain idle for more than 1,800 seconds (30 minutes) are automatically closed and their backend processes freed.
- **Token Usage Deltas:**
  - In Antigravity stream sessions, token usage counters are cumulative. Bridge tracks the previous counter and computes the per-turn delta.
  - In Codex, Bridge extracts `usage.last` to report tokens consumed specifically on that turn.
- **Crash Recovery:** An unfinished task becomes failed with `worker_interrupted`; it is never replayed. Codex sessions with a saved native thread ID can become `suspended` and resume after restart if no turn was interrupted. All sessions with an interrupted turn, and backends without verified resume support, become `interrupted`.

---

## Threat Model & Local Security

- **Nested dispatch:** Every worker process receives `AGENT_SHUTTLE_PARENT_CONTEXT=worker`, including runtimes that scrub their environment. A Bridge started inside that worker rejects task submission, cancellation, session creation and shutdown, and preference changes across MCP, A2A, and the Python API. Its default SQLite databases and MCP tickets are placed under `.agent-shuttle/nested/`; an explicit database or registry filename is redirected into a sibling `nested` directory. `/bridge/identity` reports `runtime_context` and `dispatch_enabled`. Status and result reads remain available. This prevents accidental recursive delegation through an inherited Bridge. The environment marker is not authentication: a process that removes it or connects directly to a coordinator's local A2A port is outside this protection. A host that clears the environment when starting its MCP child can also drop the marker; that host must explicitly forward `AGENT_SHUTTLE_PARENT_CONTEXT`.
- **Loopback Boundary:** Agent Shuttle relies on OS-level network loopback isolation (`127.0.0.1`). Any process running under any user account on the local machine can connect to an open A2A port.
- **Task Storage:** Standalone A2A servers use `InMemoryTaskStore` unless `--task-db` is supplied. Managed MCP task peers use SQLite under the target workspace's `.agent-shuttle` directory. `SQLiteTaskStore.list()` filters and orders indexed task metadata in SQL and reads only the requested page of task BLOBs. Task prompts, history, and artifacts are stored there without encryption; protect the workspace accordingly. Only one server may own a task database at a time. After restart, finished tasks remain readable and interrupted tasks become failed without replaying their side effects.
- **Sandbox Limits:** Tool policies (`read_only`, `workspace_write`) constrain agent actions through the harness's internal policy engine or sandbox. They are not an operating system container (like Docker or Firejail). If total isolation from the host filesystem is required, run Agent Shuttle inside an external container.
