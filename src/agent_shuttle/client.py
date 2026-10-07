"""Python task lifecycle API for Agent Shuttle and task-capable A2A peers."""

from __future__ import annotations

import asyncio
import logging
import math
import json
import uuid
import hmac
from collections.abc import AsyncIterator
from dataclasses import dataclass
from time import monotonic

import httpx
from google.protobuf.json_format import MessageToDict

from a2a.client import ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import (
    CancelTaskRequest, GetTaskRequest, Role, SendMessageConfiguration,
    SendMessageRequest, SubscribeToTaskRequest, TaskState,
)
from a2a.utils.errors import TaskNotCancelableError

from .profiles import ToolPolicy
from .structured import encode_output_schema
from .runtime_context import require_coordinator
from .local_auth import (PeerAuthenticationError, local_origin, new_nonce,
                         read_local_credential)


log = logging.getLogger(__name__)
_DEFAULT_TIMEOUT = object()


@dataclass(frozen=True)
class ShuttleResult:
    peer: str
    task_id: str | None
    context_id: str | None
    state: str
    text: str
    usage: dict[str, int] | None = None
    details: dict | None = None
    error: dict | None = None


@dataclass(frozen=True)
class ShuttleEvent:
    kind: str
    task_id: str
    state: str | None = None
    text: str = ""
    data: dict | None = None


_TERMINAL_STATES = frozenset({
    "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED", "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED",
})


@dataclass(frozen=True)
class TaskHandle:
    """A task identifier that remains usable after the submitting call returns."""

    client: "ShuttleClient"
    peer_url: str
    task_id: str
    context_id: str | None

    async def status(self) -> ShuttleResult:
        return await self.client.task_status(self.peer_url, self.task_id)

    async def wait(self, timeout: float | None = None) -> ShuttleResult:
        """Wait for a settled task; a wait timeout never cancels execution."""
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be finite and non-negative")
        deadline = None if timeout is None else monotonic() + timeout
        last = ShuttleResult(self.peer_url, self.task_id, self.context_id,
                            "TASK_STATE_SUBMITTED", "")
        while True:
            if deadline is None:
                result = await self.status()
            else:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return last
                try:
                    result = await asyncio.wait_for(self.status(), timeout=remaining)
                except asyncio.TimeoutError:
                    return last
            last = result
            if result.state in _TERMINAL_STATES:
                return result
            if deadline is not None and monotonic() >= deadline:
                return result
            delay = 0.2 if deadline is None else min(0.2, max(0, deadline - monotonic()))
            await asyncio.sleep(delay)

    async def result(self) -> ShuttleResult:
        return await self.wait()

    async def cancel(self) -> ShuttleResult:
        return await self.client.cancel_task(self.peer_url, self.task_id)

    async def events(self) -> AsyncIterator[ShuttleEvent]:
        """Observe live A2A updates; use status() to recover after disconnect."""
        async for event in self.client.task_events(self.peer_url, self.task_id):
            yield event

    async def result_page(self, cursor: int = 0, limit: int = 60000) -> dict:
        return await self.client.task_result_page(self.peer_url, self.task_id, cursor, limit)

    async def changes(self) -> dict:
        return await self.client.task_changes(self.peer_url, self.task_id)

    async def diff(self, cursor: int = 0, limit: int = 60000) -> dict:
        return await self.client.task_diff(self.peer_url, self.task_id, cursor, limit)

    async def apply_changes(self, expected_revision: str, *, allow_partial: bool = False) -> dict:
        return await self.client.apply_task_changes(self.peer_url, self.task_id,
                                                    expected_revision, allow_partial=allow_partial)

    async def discard_changes(self) -> dict:
        return await self.client.discard_task_changes(self.peer_url, self.task_id)

    async def transcript(self, cursor: int = 0, limit: int = 100) -> dict:
        return await self.client.task_transcript(self.peer_url, self.task_id, cursor, limit)


class ShuttleClient:
    def __init__(self, timeout_seconds: float = 1800, *, credentials: dict[str, str] | None = None):
        self.timeout_seconds = timeout_seconds
        self.credentials = credentials or {}

    async def _http(self, peer_url: str, *, timeout: float | None | object = _DEFAULT_TIMEOUT) -> httpx.AsyncClient:
        """Authenticate a fresh connection before adding any secret header."""
        origin = peer_url.rstrip("/")
        record = read_local_credential(origin)
        token = self.credentials.get(origin)
        if local_origin(origin) is not None and record is None and token is None:
            raise PeerAuthenticationError(
                f"No protected credential for {origin}; update the client and restart "
                "an old unprotected server"
            )
        if record is not None:
            nonce = new_nonce()
            try:
                async with httpx.AsyncClient(timeout=min(self.timeout_seconds, 10),
                                             trust_env=False) as probe:
                    response = await probe.get(origin + "/shuttle/proof", params={"nonce": nonce})
                    response.raise_for_status()
                    proof = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise PeerAuthenticationError(
                    f"Peer at {origin} did not provide a valid local credential proof"
                ) from exc
            if not isinstance(proof, dict):
                raise PeerAuthenticationError(f"Peer at {origin} returned an invalid proof")
            if (proof.get("origin") != record.origin
                    or proof.get("instance_id") != record.instance_id
                    or not isinstance(proof.get("signature"), str)
                    or not hmac.compare_digest(proof["signature"], record.signature(nonce))):
                raise PeerAuthenticationError(f"Peer at {origin} failed local credential proof")
            if token is not None and not hmac.compare_digest(token, record.token):
                raise PeerAuthenticationError(f"Explicit credential for {origin} does not match the local record")
            token = record.token
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return httpx.AsyncClient(timeout=self.timeout_seconds if timeout is _DEFAULT_TIMEOUT else timeout,
                                 headers=headers, trust_env=local_origin(origin) is None)

    def task(self, peer_url: str, task_id: str) -> TaskHandle:
        """Reopen a live-server task using the ID returned by submit()."""
        if not task_id:
            raise ValueError("task_id must be nonempty")
        return TaskHandle(self, peer_url, task_id, None)

    async def _get(self, peer_url: str, path: str) -> dict:
        async with await self._http(peer_url) as http:
            response = await http.get(peer_url.rstrip("/") + path)
            response.raise_for_status()
            return response.json()

    async def info(self, peer_url: str) -> dict:
        """Read the peer's current model catalog, effort options, and account quotas."""
        return await self._get(peer_url, "/shuttle/info")

    async def identity(self, peer_url: str) -> dict:
        """Read local server identity and safety mode without starting a model CLI."""
        try:
            return await asyncio.wait_for(
                self._get(peer_url, "/shuttle/identity"),
                timeout=min(self.timeout_seconds, 10.0),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise PeerAuthenticationError(
                    f"Shuttle at {peer_url} requires Bearer authentication; update the client "
                    "and restart an old unprotected server"
                ) from exc
            if exc.response.status_code == 404:
                raise ValueError(
                    f"Shuttle at {peer_url} lacks /shuttle/identity; restart it with the current version"
                ) from exc
            raise

    async def capabilities(self, peer_url: str) -> dict:
        return await self._get(peer_url, "/shuttle/capabilities")

    async def usage(self, peer_url: str) -> dict:
        return await self._get(peer_url, "/shuttle/usage")

    def session(
        self,
        peer_url: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
        tool_policy: str | None = None,
        output_schema: dict | None = None,
    ) -> "ShuttleSession":
        """Create an isolated conversation; use with ``async with`` for cleanup."""
        return ShuttleSession(self, peer_url, model, reasoning_effort, read_only, tool_policy, output_schema)

    async def close_session(self, peer_url: str, session_id: str) -> bool:
        require_coordinator()
        async with await self._http(peer_url) as http:
            response = await http.delete(
                peer_url.rstrip("/") + "/shuttle/sessions/" + str(uuid.UUID(session_id))
            )
            response.raise_for_status()
            return bool(response.json()["closed"])

    async def submit(
        self, peer_url: str, prompt: str, model: str | None = None, *,
        reasoning_effort: str | None = None, read_only: bool = False,
        tool_policy: str | None = None, session_id: str | None = None,
        request_id: str | None = None,
        output_schema: dict | None = None,
        workspace_mode: str = "shared",
    ) -> TaskHandle:
        """Start an A2A task and return its handle before the agent finishes."""
        require_coordinator()
        message = _request_message(prompt, model, reasoning_effort, read_only,
                                   tool_policy, session_id, request_id, output_schema,
                                   workspace_mode)
        request = SendMessageRequest(
            message=message,
            configuration=SendMessageConfiguration(return_immediately=True),
        )
        async with await self._http(peer_url) as http:
            client = await create_client(
                peer_url.rstrip("/"),
                ClientConfig(streaming=False, httpx_client=http),
                resolver_http_kwargs={"timeout": self.timeout_seconds},
            )
            try:
                last = None
                async for item in client.send_message(request):
                    last = item
                if last is None or last.WhichOneof("payload") != "task":
                    raise RuntimeError("A2A agent did not return a task")
                task = last.task
                return TaskHandle(self, peer_url, task.id, task.context_id or None)
            finally:
                await client.close()

    async def _task_call(self, peer_url: str, method: str, request) -> ShuttleResult:
        return _task_result(peer_url, await self._raw_task_call(peer_url, method, request))

    async def _raw_task_call(self, peer_url: str, method: str, request):
        async with await self._http(peer_url) as http:
            client = await create_client(
                peer_url.rstrip("/"),
                ClientConfig(streaming=False, httpx_client=http),
                resolver_http_kwargs={"timeout": self.timeout_seconds},
            )
            try:
                task = await getattr(client, method)(request)
                return task
            finally:
                await client.close()

    async def task_status(self, peer_url: str, task_id: str) -> ShuttleResult:
        return await self._task_call(peer_url, "get_task", GetTaskRequest(id=task_id))

    async def _get_task(self, peer_url: str, task_id: str):
        return await self._raw_task_call(peer_url, "get_task", GetTaskRequest(id=task_id))

    async def task_result_page(self, peer_url: str, task_id: str, cursor: int = 0, limit: int = 60000) -> dict:
        _validate_page(cursor, limit, 60000)
        result = _task_result(peer_url, await self._get_task(peer_url, task_id))
        end = min(cursor + limit, len(result.text))
        return {"task_id": task_id, "state": result.state, "text": result.text[cursor:end],
                "next_cursor": end if end < len(result.text) else None, "total_size": len(result.text),
                "usage": result.usage, "details": result.details, "error": result.error}

    async def task_changes(self, peer_url: str, task_id: str) -> dict:
        return await self._get(peer_url, f"/shuttle/tasks/{uuid.UUID(task_id)}/changes")

    async def task_diff(self, peer_url: str, task_id: str, cursor: int = 0,
                        limit: int = 60000) -> dict:
        _validate_page(cursor, limit, 60000)
        async with await self._http(peer_url) as http:
            response = await http.get(peer_url.rstrip("/") +
                                      f"/shuttle/tasks/{uuid.UUID(task_id)}/diff",
                                      params={"cursor": cursor, "limit": limit})
            response.raise_for_status()
            return response.json()

    async def apply_task_changes(self, peer_url: str, task_id: str,
                                 expected_revision: str, *, allow_partial: bool = False) -> dict:
        require_coordinator()
        async with await self._http(peer_url) as http:
            response = await http.post(peer_url.rstrip("/") +
                                       f"/shuttle/tasks/{uuid.UUID(task_id)}/apply",
                                       json={"expected_revision": expected_revision,
                                             "allow_partial": allow_partial})
            response.raise_for_status()
            return response.json()

    async def discard_task_changes(self, peer_url: str, task_id: str) -> dict:
        require_coordinator()
        async with await self._http(peer_url) as http:
            response = await http.delete(peer_url.rstrip("/") +
                                         f"/shuttle/tasks/{uuid.UUID(task_id)}/changes")
            response.raise_for_status()
            return response.json()

    async def task_transcript(self, peer_url: str, task_id: str, cursor: int = 0, limit: int = 100) -> dict:
        _validate_page(cursor, limit, 100)
        task = await self._get_task(peer_url, task_id)
        items = [{"kind": "message", "message": MessageToDict(message)} for message in task.history]
        # Status messages need not be included in history by every A2A peer.
        if task.status.HasField("message") and not any(message == task.status.message for message in task.history):
            items.append({"kind": "message", "message": MessageToDict(task.status.message)})
        items.extend({"kind": "artifact", "artifact": MessageToDict(artifact)} for artifact in task.artifacts)
        bounded = []
        for index, item in enumerate(items):
            encoded = json.dumps(item, ensure_ascii=False)
            if len(encoded) <= 24000:
                bounded.append(item)
            else:
                for offset in range(0, len(encoded), 12000):
                    bounded.append({"kind": "json_chunk", "event_index": index, "offset": offset,
                                    "text": encoded[offset:offset + 12000],
                                    "last_chunk": offset + 12000 >= len(encoded)})
        items = bounded
        end = cursor
        characters = 0
        while end < min(cursor + limit, len(items)):
            size = len(json.dumps(items[end], ensure_ascii=False))
            if characters + size > 60000:
                break
            characters += size
            end += 1
        return {"task_id": task_id, "state": TaskState.Name(task.status.state), "items": items[cursor:end],
                "next_cursor": end if end < len(items) else None, "total_size": len(items)}

    async def cancel_task(self, peer_url: str, task_id: str) -> ShuttleResult:
        require_coordinator()
        try:
            return await self._task_call(peer_url, "cancel_task", CancelTaskRequest(id=task_id))
        except TaskNotCancelableError:
            state = await self.task_status(peer_url, task_id)
            if state.state == "TASK_STATE_CANCELED":
                return state
            raise

    async def task_events(self, peer_url: str, task_id: str) -> AsyncIterator[ShuttleEvent]:
        async with await self._http(peer_url, timeout=None) as http:
            client = await create_client(
                peer_url.rstrip("/"),
                ClientConfig(streaming=True, httpx_client=http),
                resolver_http_kwargs={"timeout": self.timeout_seconds},
            )
            try:
                async for item in client.subscribe(SubscribeToTaskRequest(id=task_id)):
                    which = item.WhichOneof("payload")
                    if which == "task":
                        yield ShuttleEvent("task", task_id,
                                          TaskState.Name(item.task.status.state), data=MessageToDict(item.task))
                    elif which == "status_update":
                        status = item.status_update.status
                        yield ShuttleEvent("status", task_id, TaskState.Name(status.state),
                                          _parts(status.message.parts) if status.HasField("message") else "",
                                          MessageToDict(item.status_update))
                    elif which == "artifact_update":
                        yield ShuttleEvent("artifact", task_id, text=_parts(item.artifact_update.artifact.parts),
                                          data=MessageToDict(item.artifact_update))
            finally:
                await client.close()

    async def ask(
        self,
        peer_url: str,
        prompt: str,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        read_only: bool = False,
        tool_policy: str | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        output_schema: dict | None = None,
    ) -> ShuttleResult:
        require_coordinator()
        submission = asyncio.create_task(self.submit(
            peer_url, prompt, model, reasoning_effort=reasoning_effort,
            read_only=read_only, tool_policy=tool_policy, session_id=session_id,
            request_id=request_id,
            **({"output_schema": output_schema} if output_schema is not None else {}),
        ))
        try:
            # Keep the submission alive long enough to learn the task ID even
            # if the caller is cancelled as the backend begins its turn.
            handle = await asyncio.shield(submission)
        except asyncio.CancelledError:
            try:
                handle = await asyncio.wait_for(submission, timeout=min(10, self.timeout_seconds))
                await asyncio.wait_for(handle.cancel(), timeout=min(10, self.timeout_seconds))
            except Exception:
                log.exception("Could not cancel a task after ask() was interrupted during submit")
            raise
        try:
            result = await handle.wait(timeout=self.timeout_seconds)
            if result.state not in _TERMINAL_STATES:
                raise TimeoutError(f"Task {handle.task_id} did not finish within {self.timeout_seconds:g}s")
            return result
        except (asyncio.CancelledError, TimeoutError):
            # With an explicit task ID, the blocking convenience API can stop
            # the remote turn instead of leaving it orphaned after its caller
            # is cancelled. submit() itself remains detached by design.
            try:
                await asyncio.wait_for(handle.cancel(), timeout=min(10, self.timeout_seconds))
            except Exception:
                log.exception("Could not cancel task %s after ask() stopped waiting", handle.task_id)
            raise


class ShuttleSession:
    """Reusable per-agent conversation with explicit lifetime and pinned settings."""

    def __init__(
        self,
        client: ShuttleClient,
        peer_url: str,
        model: str | None,
        reasoning_effort: str | None,
        read_only: bool,
        tool_policy: str | None,
        output_schema: dict | None = None,
    ):
        self.client = client
        self.peer_url = peer_url
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.read_only = read_only
        self.tool_policy = tool_policy
        encoded = encode_output_schema(output_schema)
        self.output_schema = json.loads(encoded) if encoded else None
        self.id = str(uuid.uuid4())
        self._closed = False
        self._tainted = False
        self._started = False
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "ShuttleSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def ask(self, prompt: str) -> ShuttleResult:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Shuttle session is closed")
            if self._tainted:
                raise RuntimeError("Shuttle session was cancelled; start a new session")
            self._started = True
            try:
                return await self.client.ask(
                    self.peer_url,
                    prompt,
                    model=self.model,
                    reasoning_effort=self.reasoning_effort,
                    read_only=self.read_only,
                    tool_policy=self.tool_policy,
                    session_id=self.id,
                    **({"output_schema": self.output_schema} if self.output_schema is not None else {}),
                )
            except (asyncio.CancelledError, TimeoutError):
                self._tainted = True
                raise

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._started:
                await self.client.close_session(self.peer_url, self.id)


def _parts(parts) -> str:
    return "\n".join(part.text for part in parts if part.WhichOneof("content") == "text")


def _validate_page(cursor, limit, maximum):
    if (isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0
            or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum):
        raise ValueError(f"cursor must be a non-negative integer; limit must be between 1 and {maximum}")


def _request_message(prompt, model, reasoning_effort, read_only, tool_policy, session_id,
                     request_id=None, output_schema=None, workspace_mode="shared"):
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must contain text")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("model must be a nonempty string when provided")
    if reasoning_effort is not None and (
        not isinstance(reasoning_effort, str) or not reasoning_effort.strip()
    ):
        raise ValueError("reasoning_effort must be a nonempty string when provided")
    if tool_policy is not None:
        try:
            ToolPolicy(tool_policy)
        except ValueError as exc:
            raise ValueError("tool_policy must be no_tools, read_only, workspace_write, or full_access") from exc
        if read_only and tool_policy != ToolPolicy.READ_ONLY.value:
            raise ValueError("read_only conflicts with tool_policy")
    if workspace_mode not in {"shared", "isolated"}:
        raise ValueError("workspace_mode must be shared or isolated")
    if session_id is not None:
        session_id = str(uuid.UUID(session_id))
    if request_id is not None:
        request_id = str(uuid.UUID(request_id))
    message = new_text_message(prompt, context_id=session_id, role=Role.ROLE_USER)
    if request_id is not None:
        message.message_id = request_id
        message.metadata["agent_shuttle.request_id"] = request_id
    if model is not None:
        message.metadata["agent_shuttle.model"] = model.strip()
    if reasoning_effort is not None:
        message.metadata["agent_shuttle.reasoning_effort"] = reasoning_effort.strip()
    if read_only:
        message.metadata["agent_shuttle.read_only"] = True
    if tool_policy is not None:
        message.metadata["agent_shuttle.tool_policy"] = tool_policy
    if workspace_mode != "shared":
        message.metadata["agent_shuttle.workspace_mode"] = workspace_mode
    if session_id is not None:
        message.metadata["agent_shuttle.session_id"] = session_id
    schema = encode_output_schema(output_schema)
    if schema is not None:
        message.metadata["agent_shuttle.output_schema"] = schema
    return message


def _task_result(peer_url, task) -> ShuttleResult:
    state = TaskState.Name(task.status.state)
    content = "\n".join(_parts(artifact.parts) for artifact in task.artifacts).strip()
    usage = None
    details = None
    envelopes = [MessageToDict(task.metadata)]
    if task.status.HasField("message"):
        envelopes.append(MessageToDict(task.status.message.metadata))
    envelopes.extend(MessageToDict(artifact.metadata) for artifact in task.artifacts
                     if artifact.HasField("metadata"))
    for artifact_metadata in envelopes:
        if artifact_metadata:
            candidate = artifact_metadata.get("agent_shuttle.usage")
            if isinstance(candidate, dict):
                usage = {
                    key: int(value) for key, value in candidate.items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                    and value >= 0 and float(value).is_integer()
                }
            candidate_details = artifact_metadata.get("agent_shuttle.details")
            if isinstance(candidate_details, dict):
                details = candidate_details
    if not content and task.status.HasField("message"):
        content = _parts(task.status.message.parts)
    metadata = MessageToDict(task.metadata)
    error = metadata.get("agent_shuttle.error")
    if not isinstance(error, dict) and task.status.HasField("message"):
        error = MessageToDict(task.status.message.metadata).get("agent_shuttle.error")
    return ShuttleResult(peer_url, task.id, task.context_id, state, content, usage, details,
                        error if isinstance(error, dict) else None)
