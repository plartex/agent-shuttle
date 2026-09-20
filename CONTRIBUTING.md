# Contributing

1. Create a virtual environment and run `./Install.ps1` on Windows.
2. Keep account credentials, local binaries, generated MCP configs, and runtime logs out of Git.
3. Run `python -m unittest discover -s tests -v` before opening a merge request.
4. Add protocol tests for changes to A2A messages or MCP schemas.

The default test suite uses fake backends and does not consume model quota. Live harness checks require local Codex and Antigravity sign-in and should remain opt-in.
