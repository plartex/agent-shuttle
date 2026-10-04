"""Replay and live delivery of library task events."""

from __future__ import annotations

import asyncio
import json

from .library_repository import LibraryTaskRepository


_FINAL = frozenset({"completed", "failed", "canceled", "rejected"})


class EventStreamService:
    def __init__(self, repository: LibraryTaskRepository):
        self.repository = repository
        self._sinks: dict[str, object] = {}

    def attach(self, task_id: str, sink) -> None:
        self._sinks[task_id] = sink

    def detach(self, task_id: str) -> None:
        self._sinks.pop(task_id, None)

    async def publish_backend_event(self, task_id: str, event: dict) -> None:
        # The journal is authoritative even if the transport subscriber leaves.
        self.repository.update_task(task_id, "working", event)
        sink = self._sinks.get(task_id)
        if sink is not None:
            try:
                await sink(event)
            except Exception:
                self.detach(task_id)

    @staticmethod
    def _page(cursor: int, limit: int, maximum: int) -> None:
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or limit < 1 or limit > maximum:
            raise ValueError(f"cursor must be nonnegative and limit between 1 and {maximum}")

    def transcript(self, task_id: str, cursor: int = 0, limit: int = 100) -> dict:
        self._page(cursor, limit, 100)
        self.repository.task(task_id)
        rows = self.repository.events(task_id, cursor, limit)
        items = []
        remaining = 60000
        for row in rows:
            data = row["data"]
            overhead = len(row["kind"]) + 200
            if len(data) + overhead > remaining:
                if remaining < 500 and items:
                    break
                preview_size = max(0, remaining - overhead - 200)
                item_data = {"preview": data[:preview_size], "truncated": True}
                truncated = True
            else:
                item_data = json.loads(data)
                truncated = False
            items.append({"seq": row["seq"], "timestamp": row["timestamp"],
                          "kind": row["kind"], "data": item_data,
                          "data_truncated": truncated})
            remaining -= min(len(data), max(0, remaining - overhead)) + overhead
            if remaining < 500:
                break
        total = self.repository.event_count(task_id)
        next_cursor = items[-1]["seq"] + 1 if items and items[-1]["seq"] + 1 < total else None
        return {"task_id": task_id, "items": items, "next_cursor": next_cursor, "total_size": total}

    def event_page(self, task_id: str, seq: int, cursor: int = 0, limit: int = 60000) -> dict:
        self._page(cursor, limit, 60000)
        if type(seq) is not int or seq < 0:
            raise ValueError("seq must be nonnegative")
        self.repository.task(task_id)
        data = self.repository.event(task_id, seq)
        if data is None:
            raise KeyError(f"Unknown event {seq} for task {task_id}")
        end = min(cursor + limit, len(data))
        return {"task_id": task_id, "seq": seq, "text": data[cursor:end],
                "next_cursor": end if end < len(data) else None,
                "total_size": len(data)}

    async def events(self, task_id: str, cursor: int = 0):
        while True:
            page = self.transcript(task_id, cursor)
            for event in page["items"]:
                cursor = event["seq"] + 1
                yield event
            if not page["items"] and self.repository.task(task_id)["state"] in _FINAL:
                return
            if not page["items"]:
                await asyncio.sleep(0.05)
