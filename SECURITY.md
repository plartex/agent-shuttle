# Security

The A2A bridge binds to `127.0.0.1` and has no network authentication. Do not expose its ports outside the host without adding TLS and authentication. Any local process can call an A2A endpoint.

Server-owned profiles constrain model, endpoint, reasoning effort and tool policy. They are not an operating-system sandbox. `read_only` limits agent tools but cannot prevent every possible filesystem effect from runtime internals; `workspace_write` permits project mutation. Run untrusted tasks inside an OS/container sandbox. Use scoped Antigravity permission rules and review Codex approval settings before unattended use.

For Ollama, only loopback HTTP endpoints are accepted. Credentials for other providers must come from explicit environment variables and must not be stored in profile JSON. OpenCode's managed server uses a random loopback port with a generated password. Do not attach an uncontrolled OpenCode server to a security-sensitive profile. Inspect runtime logs carefully before sharing them; prompts and outputs can contain private code.

Report vulnerabilities privately through the GitLab project's security reporting channel. Do not include credentials, access tokens, or account quota payloads in public issues.
