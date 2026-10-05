"""Configured Agent Client Protocol worker over local stdio."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from pathlib import Path
from typing import Any

from .backends import BackendResponse
from .profiles import AgentProfile, ProfileSelection, ToolPolicy
from .runtime_context import worker_env
from .process_lifecycle import spawn_options, stop_async_process


ADVISORY_WARNING = (
    "ACP tool policy is advisory: this agent may use tools outside client permission requests; "
    "read_only and workspace_write are not enforced by Agent Shuttle"
)


def _plain(value: Any) -> Any:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return value
    return {"value": str(value)}


async def _stop_child(process) -> None:
    await stop_async_process(process)


def _options(response: Any) -> list[dict]:
    raw = getattr(response, "config_options", None) or []
    return [_plain(item) for item in raw]


def _offered(options: list[dict], key: str) -> tuple[str | None, set[str], str | None]:
    for option in options:
        category = option.get("category")
        if option.get("id") != key and category != ("thought_level" if key == "effort" else key):
            continue
        choices = list(option.get("options") or [])
        values = set()
        while choices:
            choice = choices.pop(0)
            if not isinstance(choice, dict):
                continue
            if isinstance(choice.get("options"), list):
                choices.extend(choice["options"])
            if isinstance(choice.get("value"), str):
                values.add(choice["value"])
        return option.get("currentValue") or option.get("current_value"), {
            value for value in values if isinstance(value, str)
        }, option.get("id")
    return None, set(), None


class _Client:
    def __init__(self, selection: ProfileSelection, workspace: Path):
        self.selection = selection
        self.workspace = workspace
        self.parts: list[str] = []
        self.events = None

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        if self.selection.tool_policy in {ToolPolicy.NO_TOOLS, ToolPolicy.READ_ONLY}:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        call = _plain(tool_call)
        locations = call.get("locations") or []
        within_workspace = bool(locations) and all(
            isinstance(item, dict) and isinstance(item.get("path"), str)
            and Path(item["path"]).is_absolute()
            and Path(item["path"]).resolve().is_relative_to(self.workspace)
            for item in locations
        )
        if self.selection.tool_policy == ToolPolicy.WORKSPACE_WRITE and not within_workspace:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        for option in options or []:
            if getattr(option, "kind", None) in {"allow_once", "allow_always"}:
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(option_id=option.option_id, outcome="selected")
                )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def session_update(self, session_id, update, **kwargs):
        data = _plain(update)
        kind = data.get("sessionUpdate") or data.get("session_update") or type(update).__name__
        content = data.get("content") or {}
        if kind in {"agent_message_chunk", "AgentMessageChunk"}:
            text = content.get("text") if isinstance(content, dict) else None
            if isinstance(text, str):
                self.parts.append(text)
        if self.events is not None:
            await self.events({"kind": str(kind), "data": data})

    async def read_text_file(self, *args, **kwargs):
        raise RuntimeError("ACP client filesystem access is unavailable")

    async def write_text_file(self, *args, **kwargs):
        raise RuntimeError("ACP client filesystem access is unavailable")

    async def create_terminal(self, *args, **kwargs):
        raise RuntimeError("ACP client terminal access is unavailable")

    async def terminal_output(self, *args, **kwargs):
        raise RuntimeError("ACP client terminal access is unavailable")

    async def release_terminal(self, *args, **kwargs):
        return None

    async def wait_for_terminal_exit(self, *args, **kwargs):
        raise RuntimeError("ACP client terminal access is unavailable")

    async def kill_terminal(self, *args, **kwargs):
        return None

    async def create_elicitation(self, *args, **kwargs):
        from acp.schema import DeclineElicitationResponse
        return DeclineElicitationResponse(action="decline")

    async def complete_elicitation(self, *args, **kwargs):
        return None

    async def ext_method(self, *args, **kwargs):
        raise RuntimeError("ACP extension methods are unavailable")

    async def ext_notification(self, *args, **kwargs):
        return None

    def on_connect(self, conn):
        return None


class AcpSession:
    def __init__(self, runtime: "AcpRuntime", selection: ProfileSelection, process, connection,
                 client: _Client, native_id: str, supports_resume: bool, stderr_task,
                 config_options: list[dict], agent_capabilities: dict):
        self.runtime = runtime
        self.selection = selection
        self.process = process
        self.connection = connection
        self.client = client
        self.native_id = native_id
        self.supports_resume = supports_resume
        self.stderr_task = stderr_task
        self.config_options = config_options
        self.agent_capabilities = agent_capabilities
        self.closed = False

    async def ask(self, prompt: str, *, on_event=None) -> BackendResponse:
        from acp.schema import TextContentBlock

        if self.closed:
            raise RuntimeError("ACP session is closed")
        self.client.parts = []
        self.client.events = on_event
        try:
            response = await self.connection.prompt(
                session_id=self.native_id, prompt=[TextContentBlock(type="text", text=prompt)]
            )
            reason = getattr(response, "stop_reason", None)
            return BackendResponse("".join(self.client.parts), None, {
                "stop_reason": getattr(reason, "value", reason),
                "warnings": [ADVISORY_WARNING],
                "tool_policy_enforcement": "advisory",
            })
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.connection.cancel(session_id=self.native_id), timeout=3)
            raise
        finally:
            self.client.events = None

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.runtime.sessions.discard(self)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.connection.close(), timeout=2)
        await _stop_child(self.process)
        self.stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.stderr_task


class AcpRuntime:
    def __init__(self, profile: AgentProfile):
        self.profile = profile
        self.sessions: set[AcpSession] = set()
        self.handshake_timeout_seconds = 30.0
        self.supports_resume_capability: bool | None = None

    async def discover(self) -> dict:
        executable = self.profile.command[0]
        candidate = Path(executable)
        if not candidate.is_absolute():
            candidate = self.profile.workspace / candidate
        found = (str(candidate.resolve()) if candidate.is_file() else
                 shutil.which(executable) if "/" not in executable and "\\" not in executable else None)
        return {"command_found": found is not None, "command": list(self.profile.command),
                "path": found, "protocol": "acp"}

    async def inspect(self) -> dict:
        """Negotiate capabilities without sending a model prompt."""
        session = await self.open_session(ProfileSelection(None, None, self.profile.default_tool_policy))
        try:
            current_model, models, _ = _offered(session.config_options, "model")
            current_effort, efforts, _ = _offered(session.config_options, "effort")
            return {**await self.discover(), "agent_capabilities": session.agent_capabilities,
                    "config_options": session.config_options,
                    "current_model": current_model,
                    "current_reasoning_effort": current_effort,
                    "advertised_models": sorted(models),
                    "advertised_reasoning_efforts": sorted(efforts),
                    "supports_resume_after_restart": session.supports_resume}
        finally:
            await session.close()

    async def _open(self, selection: ProfileSelection, native_id: str | None = None) -> AcpSession:
        from acp import PROTOCOL_VERSION, connect_to_agent
        from acp.schema import ClientCapabilities, Implementation

        discovery = await self.discover()
        if not discovery["command_found"]:
            raise FileNotFoundError(self.profile.command[0])
        process = await asyncio.create_subprocess_exec(
            discovery["path"], *self.profile.command[1:],
            cwd=str(self.profile.workspace), env=worker_env(os.environ),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024,
            **spawn_options(),
        )
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        async def drain():
            while await process.stderr.read(4096):
                pass
        stderr_task = asyncio.create_task(drain())
        client = _Client(selection, self.profile.workspace)
        connection = connect_to_agent(client, process.stdin, process.stdout)
        try:
            initialized = await asyncio.wait_for(connection.initialize(
                protocol_version=PROTOCOL_VERSION, client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="agent-shuttle", version="0.6.0"),
            ), timeout=self.handshake_timeout_seconds)
            capabilities = getattr(initialized, "agent_capabilities", None)
            supports_resume = bool(getattr(capabilities, "load_session", False))
            self.supports_resume_capability = supports_resume
            if native_id is None:
                response = await asyncio.wait_for(connection.new_session(
                    cwd=str(self.profile.workspace), mcp_servers=[]),
                    timeout=self.handshake_timeout_seconds)
                native_id = response.session_id
            else:
                if not supports_resume:
                    raise RuntimeError("ACP agent does not advertise session/load")
                response = await asyncio.wait_for(connection.load_session(
                    cwd=str(self.profile.workspace), session_id=native_id, mcp_servers=[]),
                    timeout=self.handshake_timeout_seconds)
            options = _options(response)
            for key, value in (("model", selection.model), ("effort", selection.reasoning_effort)):
                if value is None:
                    continue
                current, offered, option_id = _offered(options, key)
                if value == current:
                    continue
                if value not in offered or option_id is None:
                    raise ValueError(f"ACP agent did not advertise {key}={value!r}")
                response = await asyncio.wait_for(connection.set_config_option(
                    session_id=native_id, config_id=option_id, value=value),
                    timeout=self.handshake_timeout_seconds)
                options = _options(response) or options
            session = AcpSession(self, selection, process, connection, client, native_id,
                                 supports_resume, stderr_task, options, _plain(capabilities))
            self.sessions.add(session)
            return session
        except BaseException:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connection.close(), timeout=2)
            await _stop_child(process)
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            raise

    async def open_session(self, selection: ProfileSelection) -> AcpSession:
        return await self._open(selection)

    async def resume_session(self, native_id: str, selection: ProfileSelection) -> AcpSession:
        return await self._open(selection, native_id)

    async def close(self) -> None:
        for session in list(self.sessions):
            await session.close()
