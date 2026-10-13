"""Shared public identity for the Python library and MCP server."""

VERSION = "0.8.0"
REGISTRY_NAME = "io.github.plartex/agent-shuttle"
TITLE = "Agent Shuttle"
SUMMARY = "Python library and MCP server for delegating tasks to local AI agents"
CATALOG_DESCRIPTION = (
    "Agent Shuttle is a Python library and MCP server for agent delegation: delegate research, code review, "
    "implementation and second opinion tasks to Antigravity, Codex, Claude Code, OpenCode "
    "and configured ACP agents using existing local authentication. Discover agents, "
    "inspect models and capabilities, submit tasks and retrieve results. "
    "Делегирование задач локальным AI-агентам: исследование, ревью, реализация, второе мнение."
)
REGISTRY_DESCRIPTION = "Delegate research, reviews, edits and second opinions to Antigravity, Codex, Claude Code, OpenCode."
DELEGATION_DESCRIPTION = (
    "Delegate research, code review, implementation or a second opinion to a local AI agent: "
    "Antigravity (antigravity), Codex (codex), OpenCode (opencode), Claude Code (claude_code), "
    "or an agent_id from list_agents. Делегирование: исследование, ревью, реализация, второе мнение. "
    "Use for an actual request to have another agent perform work. Answer general explanations "
    "and advice directly; mentioning an agent or a second opinion alone does not request delegation. "
    "Shuttle manages local servers automatically. Inspect get_agent_info before selecting an "
    "enforceable tool policy; research including web access depends on that agent's capabilities. "
    "For Antigravity web research choose web_research when advertised; read_only allows local files only. "
    "Preserve the user's explicit agent and model. Use submit_task for long work."
)
