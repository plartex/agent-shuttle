"""Small Python API for sending text tasks to any A2A 1.x agent."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from a2a.client import ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import Role, SendMessageRequest, TaskState


@dataclass(frozen=True)
class BridgeResult:
    peer: str
    task_id: str | None
    context_id: str | None
    state: str
    text: str


class BridgeClient:
    def __init__(self, timeout_seconds: float = 1800):
        self.timeout_seconds = timeout_seconds

    async def _get(self, peer_url: str, path: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as http:
            response = await http.get(peer_url.rstrip("/") + path)
            response.raise_for_status()
            return response.json()

    async def info(self, peer_url: str) -> dict:
        """Read the peer's current model catalog, effort options, and account quotas."""
        return await self._get(peer_url, "/bridge/info")

    async def capabilities(self, peer_url: str) -> dict:
        return await self._get(peer_url, "/bridge/capabilities")

    async def usage(self, peer_url: str) -> dict:
        return await self._get(peer_url, "/bridge/usage")

    async def ask(
        self,
        peer_url: str,
        prompt: str,
        model: str | None = None,
        *,
        read_only: bool = False,
    ) -> BridgeResult:
        if not prompt.strip():
            raise ValueError("prompt must contain text")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("model must be a nonempty string when provided")
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as http:
            client = await create_client(
                peer_url.rstrip("/"),
                ClientConfig(streaming=False, httpx_client=http),
                resolver_http_kwargs={"timeout": self.timeout_seconds},
            )
            try:
                message = new_text_message(prompt, role=Role.ROLE_USER)
                if model is not None:
                    message.metadata["agent_bridge.model"] = model.strip()
                if read_only:
                    message.metadata["agent_bridge.read_only"] = True
                request = SendMessageRequest(message=message)
                last = None
                async for item in client.send_message(request):
                    last = item
                if last is None:
                    raise RuntimeError("A2A agent returned no response")
                which = last.WhichOneof("payload")
                if which == "message":
                    return BridgeResult(peer_url, None, last.message.context_id or None, "message", _parts(last.message.parts))
                if which != "task":
                    raise RuntimeError(f"Unexpected A2A response: {which}")
                task = last.task
                state = TaskState.Name(task.status.state)
                text = "\n".join(_parts(artifact.parts) for artifact in task.artifacts).strip()
                if not text and task.status.HasField("message"):
                    text = _parts(task.status.message.parts)
                return BridgeResult(peer_url, task.id, task.context_id, state, text)
            finally:
                await client.close()


def _parts(parts) -> str:
    return "\n".join(part.text for part in parts if part.WhichOneof("content") == "text")
