# Python package release

The normal test workflow builds wheel and sdist distributions, checks their metadata, and uses `uv` to install the built wheel on Windows, macOS, and Linux. The package smoke test checks both command entry points, an MCP initialize/tools-list exchange, and import from a separate project environment. It does not call a model.

Before publication:

1. Merge the tested changes and verify the documented Git installation on a clean machine: `uv tool install git+https://github.com/Plartex/agent-shuttle.git`. Check `agent-shuttle --help`, the `agent-shuttle-mcp` path from `uv tool dir --bin`, and an MCP client connection. This checks the public Git source rather than only a local wheel.
2. Check the `agent-shuttle` name and project ownership on PyPI immediately before release. A missing project page or pending Trusted Publisher does not reserve the name.
3. Configure PyPI Trusted Publishing for this repository and `.github/workflows/release.yml`. Configure the GitHub `pypi-production` environment with required reviewers. These external settings are not created by the workflow.
4. Run `Python release` with `publish=false` and inspect its build and installation jobs. Publication is not performed in this mode.
5. After separate release approval, select a `v<version>` tag matching `pyproject.toml` and run `Python release` with `publish=true`. The publication job requires the protected environment and uses OIDC, not a stored PyPI token.
6. Verify `uv tool install agent-shuttle` and `uv add agent-shuttle` from PyPI. Only then make the PyPI commands primary in the README and getting-started guides.

The tool installation and Python library installation have different environments. Installing the tool alone does not make `agent_shuttle` importable from an unrelated project.
