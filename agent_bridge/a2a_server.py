"""A2A 1.x server for either local coding agent."""

from __future__ import annotations

from a2a.helpers import get_message_text, new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, TaskState
from a2a.utils.errors import TaskNotCancelableError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from .backends import Backend
from .info import InfoProvider


class BridgeExecutor(AgentExecutor):
    def __init__(self, backend: Backend):
        self.backend = backend

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        prompt = get_message_text(context.message).strip()
        if not prompt:
            await updater.update_status(
                TaskState.TASK_STATE_REJECTED,
                new_text_message("A text task is required"),
            )
            return
        model = None
        if "agent_bridge.model" in context.message.metadata:
            model = context.message.metadata["agent_bridge.model"]
            if not isinstance(model, str) or not model.strip():
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    new_text_message("agent_bridge.model must be a nonempty string"),
                )
                return
            model = model.strip()
        await updater.update_status(TaskState.TASK_STATE_WORKING)
        try:
            answer = await self.backend.run(prompt, model)
        except Exception as exc:
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                new_text_message(f"{type(exc).__name__}: {exc}"),
            )
            return
        await updater.add_artifact([new_text_part(answer, media_type="text/plain")], name="result")
        await updater.update_status(TaskState.TASK_STATE_COMPLETED)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # One-shot backend calls cannot currently be cancelled safely via A2A.
        raise TaskNotCancelableError()


def make_app(name: str, backend: Backend, url: str, info_provider: InfoProvider | None = None) -> Starlette:
    skill = AgentSkill(
        id=f"run_{name}",
        name=f"Run {name} task",
        description=(
            f"Delegate a coding task to the local {name} agent and return its result. "
            "Optional message metadata agent_bridge.model selects the backend model."
        ),
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["coding", "delegation", name],
    )
    card = AgentCard(
        name=f"{name.title()} local agent",
        description=(
            f"Local {name} agent exposed through A2A by agent-bridge. "
            "Live models, reasoning efforts and account quotas are available at /bridge/info."
        ),
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url, protocol_version="1.0")],
        skills=[skill],
    )
    handler = DefaultRequestHandler(
        agent_executor=BridgeExecutor(backend),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    async def bridge_info(request):
        if info_provider is None:
            return JSONResponse({"error": "Info provider is not configured"}, status_code=503)
        path = request.url.path
        capabilities = path != "/bridge/usage"
        usage = path != "/bridge/capabilities"
        try:
            result = await info_provider.fetch(capabilities=capabilities, usage=usage)
        except Exception as exc:
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=503)
        return JSONResponse(result)

    return Starlette(
        routes=[
            Route("/bridge/info", bridge_info),
            Route("/bridge/capabilities", bridge_info),
            Route("/bridge/usage", bridge_info),
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, "/"),
        ]
    )
