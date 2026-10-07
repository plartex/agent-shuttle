"""Execution adapters for one-shot calls and reusable agent conversations."""

from __future__ import annotations

import asyncio
import json
import math
import os
from types import SimpleNamespace
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .agy_policy import SCOPED_POLICIES, ScopedAgyPolicy
from .runtime_context import worker_env
from .process_lifecycle import spawn_options, stop_async_process
from .structured import encode_output_schema, validate_output


class AntigravityPermissionDenied(RuntimeError):
    """The CLI completed a turn without permission for a requested tool."""

    session_reusable = True


class StructuredOutputError(ValueError):
    """A completed native turn failed its schema; its conversation is still usable."""

    session_reusable = True


class AntigravityAuthenticationError(RuntimeError):
    """The CLI cannot use its account from the current process context."""


def _agy_authentication_error(stderr: bytes) -> AntigravityAuthenticationError | None:
    message = stderr.decode(errors="replace").lower()
    if not any(marker in message for marker in (
        "you are not logged into antigravity",
        "authentication failed or timed out",
        "print mode: auth timed out",
    )):
        return None
    if "access is denied" in message or "permission denied" in message:
        return AntigravityAuthenticationError(
            "Antigravity CLI authentication is unavailable in this process: "
            "access to its credential store or configuration was denied. "
            "Start Agent Shuttle as a normal user outside the caller's sandbox; "
            "a successful Antigravity desktop sign-in does not grant a sandboxed CLI access."
        )
    return AntigravityAuthenticationError(
        "Antigravity CLI authentication is unavailable. Verify that the CLI is signed in "
        "from the same user account and process context used to start Agent Shuttle."
    )


def _retryable_agy_preflight_failure(stderr: bytes) -> bool:
    """Retry only a known failure before Antigravity starts the agent turn."""
    message = stderr.decode(errors="replace").lower()
    return (
        "eligibility check failed" in message
        and (
            ("failed to get profile picture" in message and "tls handshake timeout" in message)
            or ("unavailable (code 503)" in message and "service is currently unavailable" in message)
        )
    )


async def _read_agy_headless_output(process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """Drain both outputs while keeping stdin open for the CLI's entire turn."""
    stdin = getattr(process, "stdin", None)
    if stdin is None:
        return await process.communicate()
    try:
        stdout, stderr, _ = await asyncio.gather(
            process.stdout.read(), process.stderr.read(), process.wait(),
        )
        return stdout, stderr
    finally:
        try:
            stdin.close()
        except OSError:
            pass


def _check_agy_denials(result: dict) -> None:
    denied = result.get("denied_actions")
    if not denied:
        return
    actions = denied if isinstance(denied, list) else [denied]
    labels = []
    for action in actions:
        if isinstance(action, dict):
            label = action.get("display_name") or action.get("action")
        else:
            label = None
        if isinstance(label, str) and label.strip():
            labels.append(label.strip()[:100])
    names = ", ".join(labels) if labels else "an unnamed tool"
    raise AntigravityPermissionDenied(
        f"agy denied a required tool ({names}); the result may be incomplete. "
        "Review scoped permissions.allow rules in Antigravity CLI settings."
    )


def _decode_agy_result(stdout: bytes, stderr: bytes) -> str:
    """Validate headless CLI output, including its exit-zero soft-denial case."""
    try:
        result = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        detail = stderr.decode(errors="replace")[-4000:].strip()
        raise RuntimeError(f"agy returned invalid JSON: {detail or exc}") from exc
    if result.get("status") != "SUCCESS":
        raise RuntimeError(str(result.get("error") or result.get("status") or "agy failed"))
    _check_agy_denials(result)
    response = result.get("response")
    if not isinstance(response, str) or not response.strip():
        detail = stderr.decode(errors="replace")[-4000:].strip()
        message = (
            "agy reported SUCCESS but returned an empty response; in headless mode this "
            "usually means a requested tool was soft-denied by the permission policy"
        )
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message)
    return response


def _decode_agy_usage(stdout: bytes) -> dict[str, int]:
    raw_usage = json.loads(stdout).get("usage") or {}
    if not isinstance(raw_usage, dict):
        return {}
    return {
        key: value
        for key, value in raw_usage.items()
        if key in {"input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"}
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }


class Backend(Protocol):
    async def run(
        self,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
    ) -> str | "BackendResponse": ...

    async def open_session(
        self,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
    ) -> "BackendSession": ...


class BackendSession(Protocol):
    async def ask(self, prompt: str) -> str | "BackendResponse": ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class BackendResponse:
    text: str
    usage: dict[str, int]
    details: dict | None = None


def _codex_response(result) -> BackendResponse:
    """Codex exposes both cumulative and last-turn counters; report the latter."""
    last = result.usage.last if result.usage is not None else None
    usage = {} if last is None else {
        "input_tokens": last.input_tokens,
        "output_tokens": last.output_tokens,
        "total_tokens": last.total_tokens,
        "thinking_tokens": last.reasoning_output_tokens,
        "cache_read_tokens": last.cached_input_tokens,
    }
    if last is not None and last.cache_write_input_tokens is not None:
        usage["cache_write_tokens"] = last.cache_write_input_tokens
    return BackendResponse(result.final_response or "", usage)


async def _run_codex_thread(thread, prompt, on_event=None):
    if on_event is None:
        return _codex_response(await thread.run(prompt))
    turn = await thread.turn(prompt)
    stream = turn.stream()
    final = fallback = None
    usage = completed = None
    try:
        async for event in stream:
            payload = event.payload
            await on_event({"kind": event.method,
                            "text": getattr(payload, "delta", "") if isinstance(getattr(payload, "delta", ""), str) else "",
                            "data": payload.model_dump(mode="json", exclude_none=True)})
            if event.method == "item/completed":
                item = payload.item.root if hasattr(payload.item, "root") else payload.item
                if getattr(item, "type", None) == "agentMessage":
                    phase = getattr(item, "phase", None)
                    if getattr(phase, "value", phase) == "final_answer":
                        final = item.text
                    elif phase is None:
                        fallback = item.text
            elif event.method == "thread/tokenUsage/updated":
                usage = payload.token_usage
            elif event.method == "turn/completed":
                completed = payload.turn
        if completed is None:
            raise RuntimeError("Codex did not report turn completion")
        if getattr(completed.status, "value", completed.status) == "failed":
            raise RuntimeError(completed.error.message if completed.error else "Codex turn failed")
        return _codex_response(SimpleNamespace(final_response=final or fallback, usage=usage))
    except asyncio.CancelledError:
        await turn.interrupt()
        raise
    finally:
        await stream.aclose()


def _codex_sandbox(sandbox_type, read_only: bool, tool_policy: str | None):
    if read_only and tool_policy not in (None, "read_only"):
        raise ValueError("read_only conflicts with tool_policy")
    policy = tool_policy or ("read_only" if read_only else "workspace_write")
    if policy == "no_tools":
        raise ValueError("Codex cannot enforce no_tools")
    choices = {
        "read_only": sandbox_type.read_only,
        "workspace_write": sandbox_type.workspace_write,
        "full_access": sandbox_type.full_access,
    }
    try:
        return choices[policy]
    except KeyError as exc:
        raise ValueError(f"Unknown Codex tool_policy {policy!r}") from exc


class CodexBackend:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve(strict=True)

    async def run(
        self,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
        tool_policy: str | None = None,
        on_event=None,
    ) -> BackendResponse:
        from openai_codex import AsyncCodex, CodexConfig, Sandbox

        sandbox = _codex_sandbox(Sandbox, read_only, tool_policy)

        # The Windows CLI needs an explicit home in some non-interactive shells.
        codex_home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        config = CodexConfig(env=worker_env({**os.environ, "CODEX_HOME": codex_home}))
        async with AsyncCodex(config) as codex:
            thread = await codex.thread_start(
                cwd=str(self.workspace),
                model=model,
                config=(
                    {"model_reasoning_effort": reasoning_effort}
                    if reasoning_effort is not None
                    else None
                ),
                sandbox=sandbox,
            )
            return await _run_codex_thread(thread, prompt, on_event)

    async def open_session(
        self,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
        tool_policy: str | None = None,
    ) -> BackendSession:
        from openai_codex import AsyncCodex, CodexConfig, Sandbox

        sandbox = _codex_sandbox(Sandbox, read_only, tool_policy)

        codex_home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        codex = AsyncCodex(CodexConfig(env=worker_env({**os.environ, "CODEX_HOME": codex_home})))
        await codex.__aenter__()
        try:
            thread = await codex.thread_start(
                cwd=str(self.workspace),
                model=model,
                config=(
                    {"model_reasoning_effort": reasoning_effort}
                    if reasoning_effort is not None else None
                ),
                sandbox=sandbox,
            )
        except BaseException:
            await codex.__aexit__(None, None, None)
            raise
        return _CodexSession(codex, thread)

    async def resume_session(
        self,
        native_id: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
        tool_policy: str | None = None,
    ) -> BackendSession:
        from openai_codex import AsyncCodex, CodexConfig, Sandbox

        sandbox = _codex_sandbox(Sandbox, read_only, tool_policy)
        codex_home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        codex = AsyncCodex(CodexConfig(env=worker_env({**os.environ, "CODEX_HOME": codex_home})))
        await codex.__aenter__()
        try:
            thread = await codex.thread_resume(
                native_id,
                cwd=str(self.workspace),
                model=model,
                config=(
                    {"model_reasoning_effort": reasoning_effort}
                    if reasoning_effort is not None
                    else None
                ),
                sandbox=sandbox,
            )
        except BaseException:
            await codex.__aexit__(None, None, None)
            raise
        return _CodexSession(codex, thread)


class _CodexSession:
    def __init__(self, codex, thread):
        self.codex = codex
        self.thread = thread

    @property
    def native_id(self) -> str:
        return self.thread.id

    async def ask(self, prompt: str, *, on_event=None) -> BackendResponse:
        return await _run_codex_thread(self.thread, prompt, on_event)

    async def close(self) -> None:
        await self.codex.__aexit__(None, None, None)


class AntigravityCliBackend:
    """Run the official `agy` headless CLI with its configured sign-in."""

    def __init__(
        self, workspace: Path, command: str = "agy", *,
        dangerously_skip_permissions: bool = False,
        turn_timeout_seconds: float = 300,
    ):
        if not isinstance(dangerously_skip_permissions, bool):
            raise TypeError("dangerously_skip_permissions must be a boolean")
        if (not isinstance(turn_timeout_seconds, (int, float))
                or isinstance(turn_timeout_seconds, bool)
                or not math.isfinite(turn_timeout_seconds)
                or turn_timeout_seconds <= 0):
            raise ValueError("turn_timeout_seconds must be a positive finite number")
        self.workspace = workspace.resolve(strict=True)
        self.command = command
        self.dangerously_skip_permissions = dangerously_skip_permissions
        self.turn_timeout_seconds = turn_timeout_seconds

    def _tool_policy(self, read_only: bool, tool_policy: str | None) -> str | None:
        if read_only:
            if tool_policy not in (None, "read_only"):
                raise ValueError("read_only conflicts with tool_policy")
            return "read_only"
        if tool_policy in SCOPED_POLICIES or tool_policy is None:
            return tool_policy
        if tool_policy == "full_access" and self.dangerously_skip_permissions:
            return tool_policy
        raise ValueError(f"Antigravity CLI cannot enforce {tool_policy}")

    async def run(
        self,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
        tool_policy: str | None = None,
        on_event=None,
        output_schema: dict | None = None,
    ) -> BackendResponse:
        policy = self._tool_policy(read_only, tool_policy)
        schema = encode_output_schema(output_schema)
        if policy in SCOPED_POLICIES:
            session = None
            try:
                async with asyncio.timeout(self.turn_timeout_seconds):
                    session = await self.open_session(
                        model, reasoning_effort=reasoning_effort, tool_policy=policy,
                        output_schema=output_schema, on_event=on_event,
                    )
                    result = await session.ask(prompt, on_event=on_event)
                    usage = dict(result.usage or {})
                    for key, value in (session.probe_usage or {}).items():
                        usage[key] = usage.get(key, 0) + value
                    return BackendResponse(result.text, usage or None, {
                        **(result.details or {}), "policy_probe_usage": session.probe_usage, "usage_includes_policy_probe": True,
                    })
            finally:
                if session is not None:
                    await session.close()
        command = [self.command, "-p", prompt, "--output-format", "json",
                   "--print-timeout", f"{self.turn_timeout_seconds:g}s"]
        if schema is not None:
            command.extend(["--json-schema", schema])
        if self.dangerously_skip_permissions:
            command.append("--dangerously-skip-permissions")
        if model:
            command.extend(["--model", model])
        if reasoning_effort:
            command.extend(["--effort", reasoning_effort])
        deadline = asyncio.get_running_loop().time() + self.turn_timeout_seconds
        for attempt in range(3):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"agy did not return a result within {self.turn_timeout_seconds:g}s"
                )
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.workspace),
                env=worker_env(os.environ),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **spawn_options(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    _read_agy_headless_output(process), timeout=remaining,
                )
            except asyncio.TimeoutError as exc:
                await stop_async_process(process)
                raise TimeoutError(
                    f"agy did not return a result within {self.turn_timeout_seconds:g}s"
                ) from exc
            except asyncio.CancelledError:
                await stop_async_process(process)
                raise
            if process.returncode:
                authentication_error = _agy_authentication_error(stderr)
                if authentication_error is not None:
                    raise authentication_error
                retryable_preflight = _retryable_agy_preflight_failure(stderr)
                if attempt < 2 and retryable_preflight:
                    delay = (attempt + 1) * (
                        1.0 if b"unavailable (code 503)" in stderr.lower() else 0.5
                    )
                    await asyncio.sleep(min(delay, max(0, deadline - asyncio.get_running_loop().time())))
                    continue
                if retryable_preflight and b"unavailable (code 503)" in stderr.lower():
                    selected_model = model or "the CLI's default model"
                    raise RuntimeError(
                        f"Antigravity upstream service is unavailable for {selected_model} "
                        f"after {attempt + 1} attempt(s) (eligibility check HTTP 503). "
                        "The requested model was not switched; retry later or explicitly "
                        "select another model."
                    )
                raise RuntimeError(
                    f"agy failed ({process.returncode}) after {attempt + 1} attempt(s): "
                    f"{stderr.decode(errors='replace')[-4000:]}"
                )
            response = _decode_agy_result(stdout, stderr)
            if output_schema is not None:
                payload = json.loads(stdout)
                value = payload["structured_output"] if "structured_output" in payload else json.loads(response)
                response = json.dumps(validate_output(value, output_schema), ensure_ascii=False)
            details = {"preflight_retries": attempt} if attempt else None
            return BackendResponse(response, _decode_agy_usage(stdout), details)
        raise AssertionError("Antigravity retry loop exhausted unexpectedly")

    async def open_session(
        self,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
        tool_policy: str | None = None,
        output_schema: dict | None = None,
        on_event=None,
    ) -> BackendSession:
        policy = self._tool_policy(read_only, tool_policy)
        return await self._open_session(model, reasoning_effort=reasoning_effort, policy=policy,
                                        output_schema=output_schema, on_event=on_event)

    async def _resume_scoped_session(
        self, native_id: str, model: str | None = None, *,
        reasoning_effort: str | None = None, read_only: bool = False,
        tool_policy: str | None = None,
        output_schema: dict | None = None,
    ) -> BackendSession:
        """Resume a native conversation after verifying a fresh scoped tool gate."""
        if not isinstance(native_id, str) or not native_id.strip():
            raise ValueError("Antigravity conversation ID must be nonempty")
        policy = self._tool_policy(read_only, tool_policy)
        if policy not in SCOPED_POLICIES:
            raise ValueError("Antigravity resume requires a verified scoped tool policy")
        return await self._open_session(
            model, reasoning_effort=reasoning_effort, policy=policy, native_id=native_id,
            output_schema=output_schema,
        )

    async def _open_session(
        self, model: str | None, *, reasoning_effort: str | None,
        policy: str | None, native_id: str | None = None,
        output_schema: dict | None = None,
        on_event=None,
        _startup_attempt: int = 0,
    ) -> BackendSession:
        schema = encode_output_schema(output_schema)
        scoped = ScopedAgyPolicy(policy, self.workspace, output_schema=output_schema) if policy in SCOPED_POLICIES else None
        if scoped is not None:
            scoped.__enter__()
        command = [self.command, "--input-format", "stream-json", "--output-format", "stream-json", "--print-timeout", "30m"]
        if schema is not None:
            schema_argument = schema
            if scoped is not None:
                schema_path = scoped.control / "output-schema.json"
                schema_path.write_text(schema, encoding="utf-8")
                schema_argument = str(schema_path)
            command.extend(["--json-schema", schema_argument])
        if scoped is not None:
            command.extend(["--log-file", str(scoped.control / "cli.log")])
        if native_id is not None:
            command.extend(["--conversation", native_id])
        if scoped is not None:
            # Keep project hooks/plugins outside the active customization workspace.
            # The verified hook replaces CLI prompts; --skip does not bypass hooks.
            command.extend(["--disable-slash-commands", "--dangerously-skip-permissions"])
            if policy == "workspace_write":
                command.extend(["--mode", "accept-edits"])
        elif self.dangerously_skip_permissions:
            command.append("--dangerously-skip-permissions")
        if model:
            command.extend(["--model", model])
        if reasoning_effort:
            command.extend(["--effort", reasoning_effort])
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(scoped.control if scoped is not None else self.workspace),
                env=worker_env(os.environ),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=2_000_000,
                **spawn_options(),
            )
        except BaseException:
            if scoped is not None:
                scoped.__exit__()
            raise
        session = _AntigravityCliSession(
            process, policy=scoped,
            turn_timeout_seconds=self.turn_timeout_seconds if scoped is not None else 1800,
            expected_native_id=native_id,
            output_schema=output_schema,
        )
        if scoped is not None:
            try:
                await session.verify_policy(on_event=on_event)
                if native_id is not None and session.native_id != native_id:
                    raise RuntimeError("Antigravity did not confirm the resumed conversation ID")
            except (TimeoutError, RuntimeError) as exc:
                retryable = isinstance(exc, TimeoutError) or _retryable_agy_preflight_failure(session.stderr_tail)
                await session.close()
                if retryable and _startup_attempt < 2:
                    if on_event is not None:
                        await on_event({"kind": "startup_retry", "data": {
                            "attempt": _startup_attempt + 2, "reason": str(exc),
                            "user_task_dispatched": False,
                        }})
                    return await self._open_session(
                        model, reasoning_effort=reasoning_effort, policy=policy,
                        native_id=native_id, output_schema=output_schema, on_event=on_event,
                        _startup_attempt=_startup_attempt + 1,
                    )
                if retryable:
                    raise RuntimeError("Antigravity startup/policy probe failed after 3 attempts; "
                                       "the user task was not dispatched. " + str(exc)) from exc
                raise
            except BaseException:
                await session.close()
                raise
        return session


class _AntigravityCliSession:
    def __init__(self, process: asyncio.subprocess.Process, *, turn_timeout_seconds: float = 1800,
                 policy: ScopedAgyPolicy | None = None, expected_native_id: str | None = None,
                 output_schema: dict | None = None):
        self.process = process
        self.turn_timeout_seconds = turn_timeout_seconds
        self.startup_timeout_seconds = min(90, turn_timeout_seconds)
        self.previous_usage: dict[str, int] = {}
        self.stderr_tail = b""
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        self.policy = policy
        self.probe_usage = None
        self._probe_usage_reported = False
        self.native_id: str | None = None
        self.expected_native_id = expected_native_id
        self.output_schema = json.loads(encode_output_schema(output_schema)) if output_schema is not None else None

    async def verify_policy(self, *, on_event=None) -> None:
        async with asyncio.timeout(self.startup_timeout_seconds):
            prompt = self.policy.probe_prompt()
            if self.output_schema is not None:
                prompt += (
                    "\nThis CLI session has a JSON output schema. Instead of the word READY, "
                    "use the native finish tool to return a minimal placeholder object satisfying "
                    "that schema after the denied write. This is only a permission probe, not an "
                    "evaluation. If available, set batch_id=__policy_probe__ and results=[]. "
                    "For other required scalar fields use a schema-valid placeholder."
                )
            result = await self._ask_within_deadline(prompt, policy_probe=True, on_event=on_event)
        self.policy.verify_probe()
        self.probe_usage = result.usage

    async def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        while chunk := await self.process.stderr.read(4096):
            self.stderr_tail = (self.stderr_tail + chunk)[-4000:]

    async def ask(self, prompt: str, *, on_event=None) -> BackendResponse:
        if self.policy is not None:
            prompt = self.policy.task_prompt(prompt)
        try:
            async with asyncio.timeout(self.turn_timeout_seconds):
                return await self._ask_within_deadline(prompt, on_event=on_event)
        except TimeoutError as exc:
            if self.policy is not None:
                await self.close()
            elif self.process.returncode is None:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
            await self.process.wait()
            await self.stderr_task
            raise TimeoutError(
                f"agy session did not return a result within {self.turn_timeout_seconds:g}s"
            ) from exc

    async def _ask_within_deadline(self, prompt: str, *, policy_probe: bool = False, on_event=None) -> BackendResponse:
        if self.process.returncode is not None:
            await self.stderr_task
            authentication_error = _agy_authentication_error(self.stderr_tail)
            if authentication_error is not None:
                raise authentication_error
            raise RuntimeError(f"agy session exited ({self.process.returncode}): {self.stderr_tail.decode(errors='replace')}")
        assert self.process.stdin is not None and self.process.stdout is not None
        audit_offset = len(self.policy.decisions()) if self.policy is not None else 0
        event = {"event": "user", "message": {"content": prompt}}
        self.process.stdin.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.process.stdin.drain()
        while line := await self.process.stdout.readline():
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise RuntimeError(f"agy session returned invalid JSON: {exc}") from exc
            conversation_id = payload.get("conversation_id")
            if payload.get("event") == "result":
                conversation_id = (payload.get("result") or {}).get("conversation_id") or conversation_id
            if isinstance(conversation_id, str) and conversation_id:
                if self.expected_native_id is not None and self.expected_native_id != conversation_id:
                    raise RuntimeError("Antigravity resumed a different conversation")
                if self.native_id is not None and self.native_id != conversation_id:
                    raise RuntimeError("Antigravity conversation changed inside a persistent session")
                self.native_id = conversation_id
            if payload.get("event") != "result":
                if on_event is not None:
                    await on_event({"kind": str(payload.get("event", "activity")), "data": payload})
                continue
            result = payload.get("result") or {}
            if result.get("status") != "SUCCESS":
                raise RuntimeError(str(result.get("error") or result.get("status") or "agy session failed"))
            cumulative = _decode_agy_usage(json.dumps(result).encode("utf-8"))
            delta = {key: max(value - self.previous_usage.get(key, 0), 0)
                     for key, value in cumulative.items()}
            self.previous_usage = cumulative
            turn_details = {"conversation_id": self.native_id} if self.native_id is not None else {}
            if not policy_probe and self.probe_usage is not None and not self._probe_usage_reported:
                turn_details["policy_probe_usage"] = self.probe_usage
                self._probe_usage_reported = True
            if not policy_probe:
                if self.policy is not None:
                    denied = [item for item in self.policy.decisions()[audit_offset:]
                              if item.get("decision") == "deny"]
                    if denied:
                        error = AntigravityPermissionDenied(
                            f"Antigravity task policy {self.policy.policy} denied tools: "
                            + ", ".join(str(item["tool"]) for item in denied)
                            + "; the result may be incomplete."
                            + " " + "; ".join(f"{item.get('target', '')}: {item.get('reason', '')}" for item in denied)[:1500]
                        )
                        error.usage = delta
                        error.details = {**turn_details, "tool_denials": denied}
                        raise error
                _check_agy_denials(result)
            response = result.get("response")
            if self.output_schema is not None and not policy_probe:
                try:
                    value = result["structured_output"] if "structured_output" in result else json.loads(response)
                    if self.policy is not None:
                        finishes = [item for item in self.policy.decisions()[audit_offset:]
                                    if item.get("tool") == "finish" and item.get("decision") == "allow"]
                        if not finishes or finishes[-1].get("output") != value:
                            raise ValueError("no verified fresh structured result for this turn; "
                                             "submit the requested object using the native finish tool")
                    response = json.dumps(validate_output(value, self.output_schema), ensure_ascii=False)
                except (TypeError, ValueError) as exc:
                    error = StructuredOutputError(f"invalid structured output: {exc}")
                    error.usage = delta
                    error.details = turn_details
                    raise error from exc
            if not isinstance(response, str) or not response.strip():
                raise RuntimeError(
                    "agy session returned an empty response; a tool may have been soft-denied: "
                    + self.stderr_tail.decode(errors="replace")
                )
            details = {}
            if self.policy is not None:
                details = {"tool_policy": self.policy.policy, "policy_enforcement": "agy_pre_tool_use"}
            if self.native_id is not None:
                details["conversation_id"] = self.native_id
            details.update(turn_details)
            if self.output_schema is not None:
                details["structured_output"] = True
            return BackendResponse(response, delta, details or None)
        await self.stderr_task
        authentication_error = _agy_authentication_error(self.stderr_tail)
        if authentication_error is not None:
            raise authentication_error
        raise RuntimeError(f"agy session closed before result: {self.stderr_tail.decode(errors='replace')}")

    async def close(self) -> None:
        try:
            if self.process.stdin is not None and not self.process.stdin.is_closing():
                self.process.stdin.close()
            await stop_async_process(self.process)
            await self.stderr_task
        finally:
            if self.policy is not None:
                self.policy.__exit__()


def default_agy_python() -> Path:
    root = Path(__file__).resolve().parents[3]
    folder = "Scripts" if os.name == "nt" else "bin"
    name = "python.exe" if os.name == "nt" else "python"
    return root / ".venv-agy" / folder / name


class AntigravitySdkBackend:
    """Optional Antigravity SDK adapter in a separate Protobuf environment."""

    def __init__(self, workspace: Path, python: Path | None = None):
        self.workspace = workspace.resolve(strict=True)
        self.python = (python or default_agy_python()).resolve()

    async def run(
        self,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
    ) -> str:
        if read_only:
            raise ValueError("Antigravity SDK cannot enforce read-only tools")
        if not self.python.is_file():
            raise RuntimeError(f"Antigravity Python environment missing: {self.python}")
        if reasoning_effort is not None:
            raise RuntimeError(
                "Antigravity SDK mode does not expose reasoning effort; use the default CLI mode"
            )
        process = await asyncio.create_subprocess_exec(
            str(self.python),
            "-m",
            "agent_shuttle.agy_worker",
            cwd=str(self.workspace),
            env=worker_env({**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = json.dumps({"prompt": prompt, "workspace": str(self.workspace), "model": model}).encode()
        try:
            stdout, stderr = await process.communicate(payload)
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        marker = b"AGENT_SHUTTLE_RESULT="
        lines = [line[len(marker):] for line in stdout.splitlines() if line.startswith(marker)]
        if process.returncode or not lines:
            detail = stderr.decode(errors="replace")[-4000:]
            raise RuntimeError(f"Antigravity worker failed ({process.returncode}): {detail}")
        result = json.loads(lines[-1])
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Unknown Antigravity error"))
        return str(result.get("text", ""))

    async def open_session(
        self,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
    ) -> BackendSession:
        raise NotImplementedError("Persistent sessions require Antigravity CLI mode")
