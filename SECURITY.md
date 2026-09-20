# Security

The bridge binds to `127.0.0.1` and has no network authentication. Do not expose ports 8765 or 8766 outside the host without adding TLS and authentication.

The delegated agents can read and modify files according to their own sandbox and permission settings. Use scoped Antigravity permission rules and review Codex approval settings before unattended use.

Report vulnerabilities privately through the GitLab project's security reporting channel. Do not include credentials, access tokens, or account quota payloads in public issues.
