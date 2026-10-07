"""A2A 1.x server for either local coding agent."""

from __future__ import annotations

import asyncio
import os
import inspect
import math
import uuid
import json
import hmac
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic
from pathlib import Path

from a2a.helpers import get_message_text, new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (AgentCapabilities, AgentCard, AgentInterface, AgentSkill,
                       HTTPAuthSecurityScheme, SecurityScheme, SecurityRequirement, TaskState)
from a2a.utils.errors import InvalidParamsError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from .backends import (
    AntigravityCliBackend, AntigravitySdkBackend, Backend, BackendResponse,
    BackendSession, CodexBackend,
)
from .info import InfoProvider
from .profiled import ProfiledBackend
from .profiles import ToolPolicy
from .structured import encode_output_schema
from .task_library import TaskManager
from .runtime_context import NESTED_DISPATCH_ERROR, is_worker_context
from .local_auth import LocalCredential
from .metadata_migration import OLD_PREFIX


class _LoopbackAuth:
    """Reject browser and unauthenticated traffic before any A2A route runs."""

    def __init__(self, app, credential: LocalCredential):
        self.app = app
        self.credential = credential
        self.host = f"127.0.0.1:{credential.port}".encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        hosts = [value for key, value in headers if key.lower() == b"host"]
        origins = [value for key, value in headers if key.lower() == b"origin"]
        auth = [value for key, value in headers if key.lower() == b"authorization"]
        status = None
        if len(hosts) != 1 or hosts[0] != self.host:
            status = 421
        elif any(origins):
            status = 403
        elif scope.get("path") not in {"/.well-known/agent-card.json", "/shuttle/proof"}:
            prefix = b"Bearer "
            if (len(auth) != 1 or not auth[0].startswith(prefix)
                    or not hmac.compare_digest(auth[0][len(prefix):], self.credential.token.encode())):
                status = 401
        if status is None:
            return await self.app(scope, receive, send)
        response = JSONResponse({"error": "Unauthorized" if status == 401 else "Forbidden"}, status_code=status)
        if status == 401:
            response.headers["WWW-Authenticate"] = 'Bearer realm="Agent Shuttle"'
        await response(scope, receive, send)


@dataclass
class _SessionRecord:
    backend: BackendSession
    settings: tuple[str | None, str | None, bool, str | None]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=monotonic)
    closed: bool = False


class SessionManager:
    """Own backend conversations; one active turn at a time per session."""

    def __init__(self, backend: Backend, idle_seconds: float = 1800):
        self.backend = backend
        self.idle_seconds = idle_seconds
        self.sessions: dict[str, _SessionRecord] = {}
        self.cancelled_sessions: set[str] = set()
        self.lock = asyncio.Lock()

    async def run(
        self,
        session_id: str,
        prompt: str,
        model: str | None,
        reasoning_effort: str | None,
        read_only: bool,
        tool_policy: str | None = None,
        on_event=None,
    ) -> str | BackendResponse:
        settings = (model, reasoning_effort, read_only, tool_policy)
        async with self.lock:
            if session_id in self.cancelled_sessions:
                raise RuntimeError("Session was closed after cancellation; start a new session")
            record = self.sessions.get(session_id)
            if record is None:
                kwargs = {"reasoning_effort": reasoning_effort, "read_only": read_only}
                if tool_policy is not None:
                    kwargs["tool_policy"] = tool_policy
                session = await self.backend.open_session(model, **kwargs)
                record = _SessionRecord(session, settings)
                self.sessions[session_id] = record
            elif record.settings != settings:
                raise ValueError("Model, reasoning effort, and tool policy cannot change within a session")
            record.last_used = monotonic()
        async with record.lock:
            if record.closed:
                raise RuntimeError("Session was closed during a concurrent request")
            record.last_used = monotonic()
            try:
                kwargs = {"on_event": on_event} if "on_event" in inspect.signature(record.backend.ask).parameters else {}
                return await record.backend.ask(prompt, **kwargs)
            except (asyncio.CancelledError, TimeoutError):
                # The provider may have performed side effects before its turn
                # was interrupted. Never continue that same native conversation.
                record.closed = True
                self.cancelled_sessions.add(session_id)
                try:
                    await record.backend.close()
                finally:
                    raise
            finally:
                record.last_used = monotonic()

    async def close(self, session_id: str) -> bool:
        async with self.lock:
            record = self.sessions.pop(session_id, None)
        if record is None:
            return False
        async with record.lock:
            if not record.closed:
                record.closed = True
                await record.backend.close()
        return True

    async def reap_idle(self) -> None:
        cutoff = monotonic() - self.idle_seconds
        async with self.lock:
            expired = [
                (session_id, record) for session_id, record in self.sessions.items()
                if record.last_used < cutoff and not record.lock.locked()
            ]
            for session_id, _ in expired:
                self.sessions.pop(session_id)
        for _, record in expired:
            async with record.lock:
                if not record.closed:
                    record.closed = True
                    await record.backend.close()

    async def close_all(self) -> None:
        async with self.lock:
            session_ids = list(self.sessions)
        for session_id in session_ids:
            await self.close(session_id)


class ShuttleExecutor(AgentExecutor):
    def __init__(self, task_manager: TaskManager, agent_id: str):
        self.task_manager = task_manager
        self.agent_id = agent_id

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        prompt = get_message_text(context.message).strip()
        if not prompt and not context.current_task:
            await event_queue.enqueue_event(new_text_message("A text task is required"))
            return
        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        if any(key.startswith(OLD_PREFIX) for key in context.message.metadata):
            await updater.update_status(
                TaskState.TASK_STATE_REJECTED,
                new_text_message("Legacy A2A metadata keys are unsupported; use agent_shuttle.*"),
            )
            return
        if not prompt:
            await updater.update_status(
                TaskState.TASK_STATE_REJECTED,
                new_text_message("A text task is required"),
            )
            return
        model = None
        if "agent_shuttle.model" in context.message.metadata:
            model = context.message.metadata["agent_shuttle.model"]
            if not isinstance(model, str) or not model.strip():
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    new_text_message("agent_shuttle.model must be a nonempty string"),
                )
                return
            model = model.strip()
        reasoning_effort = None
        if "agent_shuttle.reasoning_effort" in context.message.metadata:
            reasoning_effort = context.message.metadata["agent_shuttle.reasoning_effort"]
            if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    new_text_message("agent_shuttle.reasoning_effort must be a nonempty string"),
                )
                return
            reasoning_effort = reasoning_effort.strip()
        read_only = False
        if "agent_shuttle.read_only" in context.message.metadata:
            read_only = context.message.metadata["agent_shuttle.read_only"]
        if not isinstance(read_only, bool):
            await updater.update_status(
                TaskState.TASK_STATE_REJECTED,
                new_text_message("agent_shuttle.read_only must be a boolean"),
            )
            return
        tool_policy = None
        if "agent_shuttle.tool_policy" in context.message.metadata:
            tool_policy = context.message.metadata["agent_shuttle.tool_policy"]
            if not isinstance(tool_policy, str) or tool_policy not in {p.value for p in ToolPolicy}:
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    new_text_message("agent_shuttle.tool_policy is invalid"),
                )
                return
            if read_only and tool_policy != ToolPolicy.READ_ONLY.value:
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    new_text_message("read_only conflicts with tool_policy"),
                )
                return
        session_id = (
            context.message.metadata["agent_shuttle.session_id"]
            if "agent_shuttle.session_id" in context.message.metadata else None
        )
        if session_id is not None:
            try:
                session_id = str(uuid.UUID(session_id))
            except (TypeError, ValueError, AttributeError):
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    new_text_message("agent_shuttle.session_id must be a UUID"),
                )
                return
            if context.message.context_id != session_id:
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    new_text_message("session_id must match the A2A context_id"),
                )
                return
        output_schema = None
        if "agent_shuttle.output_schema" in context.message.metadata:
            try:
                raw = context.message.metadata["agent_shuttle.output_schema"]
                if not isinstance(raw, str):
                    raise ValueError("output_schema must be a serialized JSON schema")
                output_schema = json.loads(raw)
                encode_output_schema(output_schema)
            except (TypeError, ValueError) as exc:
                await updater.update_status(TaskState.TASK_STATE_REJECTED, new_text_message(str(exc)))
                return
        workspace_mode = (context.message.metadata["agent_shuttle.workspace_mode"]
                          if "agent_shuttle.workspace_mode" in context.message.metadata else "shared")
        if workspace_mode not in {"shared", "isolated"}:
            await updater.update_status(TaskState.TASK_STATE_REJECTED,
                                        new_text_message("Invalid workspace_mode"))
            return
        await self._execute_library(task, updater, prompt, model, reasoning_effort,
                                    tool_policy or ("read_only" if read_only else None), session_id,
                                    (context.message.metadata["agent_shuttle.request_id"]
                                     if "agent_shuttle.request_id" in context.message.metadata else None),
                                    output_schema, workspace_mode)

    async def _execute_library(self, task, updater, prompt, model, reasoning_effort,
                               tool_policy, session_id, request_id, output_schema=None,
                               workspace_mode="shared"):
        manager = self.task_manager
        assert manager is not None and self.agent_id is not None

        async def on_event(event):
            message = new_text_message(str(event.get("text", "")) or str(event.get("kind", "activity")))
            message.metadata["agent_shuttle.event"] = event
            await updater.update_status(TaskState.TASK_STATE_WORKING, message)

        try:
            if session_id is not None:
                await manager.ensure_session(session_id, self.agent_id, model=model,
                                             reasoning_effort=reasoning_effort, tool_policy=tool_policy,
                                             output_schema=output_schema)
            core_task = await manager.dispatch(self.agent_id, prompt, model=model,
                                               reasoning_effort=reasoning_effort,
                                               tool_policy=tool_policy, session_id=session_id,
                                               output_schema=output_schema,
                                               workspace_mode=workspace_mode,
                                               request_id=request_id, task_id=task.id,
                                               event_sink=on_event)
        except Exception as exc:
            error = {"code": "backend_error", "type": type(exc).__name__,
                     "message": str(exc), "retryable": False}
            await updater.update_status(TaskState.TASK_STATE_FAILED,
                                        new_text_message(f"{type(exc).__name__}: {exc}"),
                                        metadata={"agent_shuttle.error": error})
            return
        await updater.update_status(TaskState.TASK_STATE_WORKING)
        try:
            result = await core_task.result()
        except asyncio.CancelledError:
            await core_task.cancel()
            raise
        metadata = {}
        if result.usage:
            metadata["agent_shuttle.usage"] = result.usage
        if result.details:
            metadata["agent_shuttle.details"] = result.details
        changes = await core_task.changes()
        if changes is not None:
            metadata["agent_shuttle.change"] = await changes.info()
        if result.state == "completed":
            await updater.add_artifact([new_text_part(result.text, media_type="text/plain")],
                                       name="result", metadata=metadata or None)
            await updater.update_status(TaskState.TASK_STATE_COMPLETED)
        elif result.state == "failed":
            error = result.error or {"code": "backend_error", "message": "Unknown worker error"}
            await updater.update_status(TaskState.TASK_STATE_FAILED,
                                        new_text_message(f"{error.get('type', 'Error')}: {error['message']}"),
                                        metadata={**metadata, "agent_shuttle.error": error})
        elif result.state == "canceled":
            await updater.update_status(TaskState.TASK_STATE_CANCELED,
                                        metadata=metadata or None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # The A2A active-task manager cancels and joins the producer after this
        # hook returns. Backend coroutines own their native cleanup on
        # CancelledError; a terminal CANCELED state is written only after the
        # producer has finished winding down.
        return None


class _IdempotentRequestHandler(DefaultRequestHandler):
    """Deduplicate explicitly keyed submissions within this server lifetime."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._worker_context = is_worker_context()
        self._request_lock = asyncio.Lock()
        self._requests: dict[tuple[str, str], tuple[bytes, asyncio.Task]] = {}

    async def on_message_send(self, params, context):
        if self._worker_context:
            raise InvalidParamsError(NESTED_DISPATCH_ERROR)
        request_id = (
            params.message.metadata["agent_shuttle.request_id"]
            if "agent_shuttle.request_id" in params.message.metadata else None
        )
        if request_id is None:
            return await super().on_message_send(params, context)
        try:
            request_id = str(uuid.UUID(request_id))
        except (TypeError, ValueError, AttributeError):
            raise InvalidParamsError("agent_shuttle.request_id must be a UUID") from None
        if params.message.message_id != request_id:
            raise InvalidParamsError("message_id must match agent_shuttle.request_id")
        fingerprint = params.SerializeToString(deterministic=True)
        key = (context.user.user_name, request_id)
        async with self._request_lock:
            previous = self._requests.get(key)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise InvalidParamsError("request_id is already bound to another request")
                submitted = previous[1]
            else:
                lookup = getattr(self.task_store, "request_task", None)
                if lookup is not None:
                    stored = await lookup(request_id, fingerprint, context)
                    if stored is not None:
                        return stored
                    task = new_task_from_user_message(params.message)
                    task_id, created = await self.task_store.bind_request(request_id, fingerprint, task, context)
                    if not created:
                        return await self.task_store.get(task_id, context)
                    params = type(params).FromString(params.SerializeToString())
                    params.message.task_id = task_id
                    params.message.context_id = task.context_id
                submitted = asyncio.create_task(super().on_message_send(params, context))
                self._requests[key] = (fingerprint, submitted)
        # A dropped HTTP caller must not abort the only copy of its task.
        return await asyncio.shield(submitted)

    async def on_cancel_task(self, params, context):
        if self._worker_context:
            raise InvalidParamsError(NESTED_DISPATCH_ERROR)
        return await super().on_cancel_task(params, context)


def make_app(name: str, backend: Backend, url: str, info_provider: InfoProvider | None = None,
             *, task_store=None, execution_timeout_seconds: float = 1800,
             stall_timeout_seconds: float = 1800,
             credential: LocalCredential | None = None, publish_credential: bool = False,
             backend_factory=None) -> Starlette:
    for budget in (execution_timeout_seconds, stall_timeout_seconds):
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0:
            raise ValueError("worker budgets must be positive finite numbers")
    credential = credential or LocalCredential.fresh(url)
    skill = AgentSkill(
        id=f"run_{name}",
        name=f"Run {name} task",
        description=(
            f"Delegate a coding task to the local {name} agent and return its result. "
            "Optional message metadata agent_shuttle.model and agent_shuttle.reasoning_effort "
            "select the backend model and reasoning effort."
        ),
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["coding", "delegation", name],
    )
    card = AgentCard(
        name=f"{name.title()} local agent",
        description=(
            f"Local {name} agent exposed through A2A by Agent Shuttle. "
            "Live models, reasoning efforts and account quotas are available at /shuttle/info."
        ),
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url, protocol_version="1.0")],
        skills=[skill],
    )
    card.security_schemes["agentShuttleBearer"].CopyFrom(SecurityScheme(
        http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer")))
    card.security_requirements.append(SecurityRequirement(schemes={"agentShuttleBearer": {}}))
    library_database = getattr(task_store, "path", None)
    if isinstance(library_database, Path):
        library_database = library_database.with_suffix(".library.sqlite3")
    backend_workspace = getattr(backend, "workspace", None)
    if not isinstance(backend_workspace, (str, Path)):
        backend_workspace = Path.cwd()
    manager = TaskManager({name: backend}, workspace=backend_workspace,
                          database=library_database, memory=library_database is None,
                          backend_factories={name: backend_factory} if backend_factory else None,
                          execution_timeout_seconds=execution_timeout_seconds,
                          stall_timeout_seconds=stall_timeout_seconds)
    executor = ShuttleExecutor(manager, name)
    handler = _IdempotentRequestHandler(
        agent_executor=executor,
        task_store=task_store or InMemoryTaskStore(),
        agent_card=card,
    )
    def identity_data() -> dict:
        if isinstance(backend, ProfiledBackend):
            backend_name = backend.profile.runtime
            workspace = backend.profile.workspace
        else:
            backend_name = (
                "codex_app_server" if isinstance(backend, CodexBackend) else
                "agy_cli" if isinstance(backend, AntigravityCliBackend) else
                "antigravity_sdk" if isinstance(backend, AntigravitySdkBackend) else name
            )
            workspace = getattr(backend, "workspace", None)
        result = {"agent": name, "backend": backend_name, "pid": os.getpid(),
                  "runtime_context": manager.runtime_context,
                  "dispatch_enabled": manager.runtime_context == "coordinator"}
        database = getattr(handler.task_store, "path", None)
        result["task_storage"] = "sqlite" if isinstance(database, Path) else "memory"
        result["task_db_path"] = str(database) if isinstance(database, Path) else None
        result["supported_tool_policies"] = []
        result["default_tool_policy"] = None
        result["structured_output"] = isinstance(backend, AntigravityCliBackend)
        if isinstance(backend, ProfiledBackend):
            result["max_tool_policy"] = backend.profile.max_tool_policy.value
            policies = list(ToolPolicy)
            maximum = policies.index(backend.profile.max_tool_policy)
            accepted = [p.value for p in policies[:maximum + 1]]
            if backend.profile.runtime == "acp":
                result["supported_tool_policies"] = []
                result["advisory_tool_policies"] = accepted
                result["tool_policy_enforcement"] = "advisory"
                result["tool_policy_notes"] = (
                    "ACP agents may use tools outside client permission requests. "
                    "Requested read_only and workspace_write policies are not enforced."
                )
            else:
                result["supported_tool_policies"] = accepted
            result["default_tool_policy"] = backend.profile.default_tool_policy.value
        elif isinstance(backend, CodexBackend):
            result["supported_tool_policies"] = ["read_only", "workspace_write", "full_access"]
            result["default_tool_policy"] = "workspace_write"
        if isinstance(workspace, Path):
            result["workspace"] = str(workspace.resolve(strict=True))
        result["read_only_tools"] = (
            isinstance(backend, (CodexBackend, AntigravityCliBackend))
            or isinstance(backend, ProfiledBackend)
            and backend.profile.runtime != "acp"
            and backend.profile.max_tool_policy in {
                ToolPolicy.READ_ONLY, ToolPolicy.WORKSPACE_WRITE, ToolPolicy.FULL_ACCESS,
            }
        )
        result["workspace_modes"] = (["shared", "isolated"]
                                     if backend_factory and manager._enforces_workspace(name)
                                     else ["shared"])
        if isinstance(backend, AntigravityCliBackend):
            result["supported_tool_policies"] = ["no_tools", "read_only", "workspace_write"]
            result["tool_policy_enforcement"] = "agy_pre_tool_use"
            if backend.dangerously_skip_permissions:
                result["supported_tool_policies"].append("full_access")
                result["default_tool_policy"] = "full_access"
            result["tool_policy_notes"] = (
                ("This server auto-approves all tools. " if backend.dangerously_skip_permissions
                 else "With no explicit policy, agy uses its settings; headless workspace file writes may be allowed. ")
                + "Explicit scoped policies use a verified per-conversation PreToolUse hook. "
                "workspace_write allows native file edits only; shell, MCP, subagents and "
                "external paths are blocked. This is tool gating, not an OS process sandbox."
            )
            result["agy_permission_mode"] = (
                "all" if backend.dangerously_skip_permissions else "settings"
            )
            result["agy_turn_timeout_seconds"] = backend.turn_timeout_seconds
        return result

    async def shuttle_identity(request):
        return JSONResponse(identity_data())

    async def shuttle_proof(request):
        nonce = request.query_params.get("nonce", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", nonce):
            return JSONResponse({"error": "nonce must be a 32-byte base64url value"}, status_code=400)
        return JSONResponse({"instance_id": credential.instance_id,
                             "origin": credential.origin, "signature": credential.signature(nonce)})

    async def shuttle_info(request):
        if info_provider is None:
            return JSONResponse({"error": "Info provider is not configured"}, status_code=503)
        path = request.url.path
        capabilities = path != "/shuttle/usage"
        usage = path != "/shuttle/capabilities"
        try:
            result = await info_provider.fetch(capabilities=capabilities, usage=usage)
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=503)
        if capabilities:
            result.update(identity_data())
        return JSONResponse(result)

    async def close_session(request):
        if manager.runtime_context == "worker":
            return JSONResponse({"error": NESTED_DISPATCH_ERROR,
                                 "code": "nested_dispatch_disabled"}, status_code=403)
        try:
            session_id = str(uuid.UUID(request.path_params["session_id"]))
        except ValueError:
            return JSONResponse({"error": "session_id must be a UUID"}, status_code=400)
        try:
            closed = await manager.end_session(session_id)
        except KeyError:
            closed = False
        return JSONResponse({"closed": closed})

    async def change_info(request):
        try:
            return JSONResponse(await manager.change_info(request.path_params["task_id"]))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def change_diff(request):
        try:
            cursor = int(request.query_params.get("cursor", "0"))
            limit = int(request.query_params.get("limit", "60000"))
            return JSONResponse(await manager.change_diff(request.path_params["task_id"], cursor, limit))
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def task_events_page(request):
        try:
            cursor = int(request.query_params.get("cursor", "0"))
            limit = int(request.query_params.get("limit", "100"))
            return JSONResponse(await manager.transcript(request.path_params["task_id"], cursor, limit))
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def task_event_page(request):
        try:
            seq = int(request.path_params["seq"])
            cursor = int(request.query_params.get("cursor", "0"))
            limit = int(request.query_params.get("limit", "60000"))
            return JSONResponse(await manager.event_page(request.path_params["task_id"],
                                                         seq, cursor, limit))
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def apply_change(request):
        try:
            body = await request.json()
            return JSONResponse(await manager.apply_change(
                request.path_params["task_id"], body["expected_revision"],
                allow_partial=body.get("allow_partial", False)))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def discard_change(request):
        try:
            return JSONResponse(await manager.discard_change(request.path_params["task_id"]))
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @asynccontextmanager
    async def lifespan(app):
        check_ready = getattr(info_provider, "check_ready", None)
        if check_ready is not None:
            await check_ready()
        acquire = getattr(handler.task_store, "acquire_owner", None)
        if acquire is not None:
            acquire()
        async def reap_loop():
            while True:
                await asyncio.sleep(60)
                await manager.reap_idle_sessions()

        janitor = None
        try:
            recover = getattr(handler.task_store, "recover_interrupted", None)
            if recover is not None:
                await recover()
            async with manager:
                janitor = asyncio.create_task(reap_loop())
                try:
                    if publish_credential:
                        credential.publish()
                    yield
                finally:
                    if publish_credential:
                        credential.remove_if_owned()
                    janitor.cancel()
                    try:
                        await janitor
                    except asyncio.CancelledError:
                        pass
                    await handler._active_task_registry.aclose()
        finally:
            try:
                shutdown = getattr(backend, "close", None)
                if shutdown is not None:
                    await shutdown()
            finally:
                close_store = getattr(handler.task_store, "close", None)
                if close_store is not None:
                    close_store()

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/shuttle/proof", shuttle_proof),
            Route("/shuttle/identity", shuttle_identity),
            Route("/shuttle/info", shuttle_info),
            Route("/shuttle/capabilities", shuttle_info),
            Route("/shuttle/usage", shuttle_info),
            Route("/shuttle/sessions/{session_id}", close_session, methods=["DELETE"]),
            Route("/shuttle/tasks/{task_id}/changes", change_info),
            Route("/shuttle/tasks/{task_id}/diff", change_diff),
            Route("/shuttle/tasks/{task_id}/events", task_events_page),
            Route("/shuttle/tasks/{task_id}/events/{seq}", task_event_page),
            Route("/shuttle/tasks/{task_id}/apply", apply_change, methods=["POST"]),
            Route("/shuttle/tasks/{task_id}/changes", discard_change, methods=["DELETE"]),
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, "/"),
        ]
    )
    app.state.task_manager = manager
    app.state.local_credential = credential
    app.add_middleware(_LoopbackAuth, credential=credential)
    return app
