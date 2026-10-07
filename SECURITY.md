# Security Policy

[Russian version / Русская версия](SECURITY.ru.md)

This document outlines the security architecture, trust boundaries, known operational risks, and reporting procedures for Agent Shuttle.

---

## Localhost Binding & Authentication

- **Loopback Only:** Agent Shuttle servers bind strictly to `127.0.0.1` (loopback).
- **Bearer by default:** Each server start creates a fresh local Bearer credential. All task and `/shuttle/*` endpoints require it; only the public Agent Card and `/shuttle/proof` are unauthenticated. The server also checks the exact `Host` and rejects requests with an `Origin` header.
- **Credential location:** The per-port record is outside the project, under `%LOCALAPPDATA%\AgentShuttle\run` on Windows and `$XDG_RUNTIME_DIR/agent-shuttle` or `~/.local/state/agent-shuttle/run` on POSIX. The directory and file are restricted to the current OS user. Python and CLI clients discover it automatically and verify a nonce proof before sending Bearer. Other A2A clients can read the record as that user and send standard HTTP Bearer.
- **Trust boundary:** This protects against other OS users and requests from websites. A process under the same user account can read the record and is outside this boundary. Remote access needs a separate TLS and credential architecture; keep the server bound to `127.0.0.1`.

---

## Explicit Risks of `full_access`

The `full_access` policy (and `--agy-dangerously-skip-permissions` for Antigravity) completely removes tool approval gates:

1. **Server-Wide Scope:**  
   Enabling `full_access` affects the entire running Shuttle server instance and all subsequent task turns. It is not confined to a single request or directory.
2. **Arbitrary Command Execution:**  
   Under `full_access`, the agent can execute shell commands, read/write files outside the workspace, and mutate host system configuration with the permissions of the user running the process.
3. **Untrusted Tasks:**  
   Never pass untrusted code, external prompt injections, or unverified instructions to an agent running under `full_access`. If you must analyze untrusted code, run the entire Agent Shuttle setup inside an isolated virtual machine or container sandbox.

---

## Tool Policies vs. Operating System Sandboxes

- Agent Shuttle tool policies (`no_tools`, `read_only`, `workspace_write`) constrain tool selection at the harness policy level.
- **They are not an operating system sandbox.** While `read_only` prevents tool-assisted modifications, internal runtime processes might still create temporary files or caches.
- **Antigravity CLI Limitation:** Antigravity CLI does not provide a reliable read-only tool sandbox in headless mode. Agent Shuttle deliberately rejects `read_only` and `no_tools` requests for Antigravity before model execution to prevent false assumptions of safety.
- For true filesystem and network containment, run tasks within an OS container (e.g. Docker, Podman) or hypervisor sandbox.

---

## Credential and Secret Management

- **No Secrets in Profiles:** Do not hardcode API keys, passwords, or authentication tokens into `profile.json` files. Use the `credential_env` field to reference environment variables.
- **Loopback Ollama Restriction:** Agent Shuttle enforces that Ollama endpoints must use local HTTP loopback (`127.0.0.1`, `localhost`, `::1`). Remote unauthenticated Ollama endpoints are rejected.
- **Runtime Logs:** Inspect diagnostic logs before sharing them; prompts, responses, and stack traces may contain private source code or environment variables.

---

## Vulnerability Reporting

- **Reporting Channel:**  
  No dedicated private security reporting email address is currently configured for this repository.
- If you discover a potential security vulnerability, use GitHub's private vulnerability reporting feature if it is enabled for the repository, or contact the repository owner privately.
- **Responsible Disclosure:**  
  Do **not** disclose security vulnerabilities, access tokens, API credentials, or exploitable payloads in public issue discussions.
