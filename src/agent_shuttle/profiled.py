"""Profile-driven execution boundary shared by new agent runtimes."""

from __future__ import annotations

from datetime import datetime, timezone
import inspect
from typing import Protocol

from .backends import BackendResponse, BackendSession
from .profiles import AgentProfile, ProfileSelection, ToolPolicy


class AgentRuntime(Protocol):
    async def open_session(self, selection: ProfileSelection) -> BackendSession: ...

    async def resume_session(self, native_id: str, selection: ProfileSelection) -> BackendSession: ...

    async def discover(self) -> dict: ...

    async def close(self) -> None: ...


class ProfiledBackend:
    """Validate client choices against a server-owned profile before execution."""

    def __init__(self, profile: AgentProfile, runtime: AgentRuntime):
        self.profile = profile
        self.runtime = runtime
        self.can_attempt_resume_after_restart = profile.runtime == "acp"

    @property
    def supports_resume_after_restart(self) -> bool:
        return self.profile.runtime == "acp" and bool(
            getattr(self.runtime, "supports_resume_capability", False)
        )

    def _selection(
        self, model: str | None, reasoning_effort: str | None,
        read_only: bool, tool_policy: str | None,
    ) -> ProfileSelection:
        if read_only:
            if tool_policy not in (None, ToolPolicy.READ_ONLY.value):
                raise ValueError("read_only conflicts with tool_policy")
            tool_policy = ToolPolicy.READ_ONLY.value
        return self.profile.resolve(model, reasoning_effort, tool_policy)

    async def run(
        self, prompt: str, model: str | None = None, *,
        reasoning_effort: str | None = None, read_only: bool = False,
        tool_policy: str | None = None,
        on_event=None,
    ) -> str | BackendResponse:
        session = await self.open_session(
            model, reasoning_effort=reasoning_effort,
            read_only=read_only, tool_policy=tool_policy,
        )
        try:
            if on_event is not None and "on_event" in inspect.signature(session.ask).parameters:
                answer = await session.ask(prompt, on_event=on_event)
            else:
                answer = await session.ask(prompt)
        except BaseException:
            try:
                await session.close()
            except Exception:
                pass
            raise
        else:
            await session.close()
            return answer

    async def open_session(
        self, model: str | None = None, *, reasoning_effort: str | None = None,
        read_only: bool = False, tool_policy: str | None = None,
    ) -> BackendSession:
        selection = self._selection(model, reasoning_effort, read_only, tool_policy)
        return await self.runtime.open_session(selection)

    async def resume_session(
        self, native_id: str, model: str | None = None, *, reasoning_effort: str | None = None,
        read_only: bool = False, tool_policy: str | None = None,
    ) -> BackendSession:
        if not self.can_attempt_resume_after_restart:
            raise RuntimeError("This profiled runtime cannot resume after restart")
        selection = self._selection(model, reasoning_effort, read_only, tool_policy)
        return await self.runtime.resume_session(native_id, selection)

    async def close(self) -> None:
        await self.runtime.close()


class ProfiledInfo:
    def __init__(self, profile: AgentProfile, runtime: AgentRuntime):
        self.profile = profile
        self.runtime = runtime

    async def fetch(self, *, capabilities: bool = True, usage: bool = True) -> dict:
        result = {
            "agent": self.profile.id,
            "backend": self.profile.runtime,
            "provider": self.profile.provider,
            "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if capabilities:
            models = [
                f"{self.profile.provider}/{model}" if self.profile.runtime == "opencode" else model
                for model in self.profile.allowed_models
            ]
            result["capabilities"] = {
                "selected_model": (models[self.profile.allowed_models.index(self.profile.default_model)]
                                   if self.profile.default_model is not None else None),
                "models": models,
                "reasoning_efforts": list(self.profile.reasoning_efforts),
                "default_tool_policy": self.profile.default_tool_policy.value,
                "max_tool_policy": self.profile.max_tool_policy.value,
                "sessions": True,
            }
            result["runtime"] = (await self.runtime.inspect() if self.profile.runtime == "acp"
                                 else await self.runtime.discover())
            if self.profile.runtime == "acp":
                result["supports_resume_after_restart"] = result["runtime"][
                    "supports_resume_after_restart"
                ]
                advertised = set(result["runtime"]["advertised_models"])
                result["capabilities"]["models"] = sorted(
                    advertised.intersection(self.profile.allowed_models) if self.profile.allowed_models
                    else advertised
                )
                result["capabilities"]["selected_model"] = (
                    self.profile.default_model or result["runtime"]["current_model"]
                )
                advertised_efforts = set(result["runtime"]["advertised_reasoning_efforts"])
                result["capabilities"]["reasoning_efforts"] = sorted(
                    advertised_efforts.intersection(self.profile.reasoning_efforts)
                    if self.profile.reasoning_efforts else advertised_efforts
                )
                result["capabilities"]["sessions_resume_after_restart"] = result["runtime"][
                    "supports_resume_after_restart"
                ]
                result["capabilities"]["tool_policy_enforcement"] = "advisory"
                result["capabilities"]["advisory_tool_policies"] = [
                    p.value for p in list(ToolPolicy)[:list(ToolPolicy).index(self.profile.max_tool_policy) + 1]
                ]
                result["capabilities"]["warnings"] = [
                    "ACP tool policies are advisory; the agent may bypass client permission requests"
                ]
                result["tool_policy_enforcement"] = "advisory"
                result["advisory_tool_policies"] = result["capabilities"]["advisory_tool_policies"]
                result["warnings"] = result["capabilities"]["warnings"]
        if usage:
            result["usage"] = {
                "available": False,
                "reason": "Runtime account quotas are not available through this profile",
            }
        return result
