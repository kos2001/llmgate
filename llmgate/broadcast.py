"""Optional usage broadcast: POST one JSON event per request to a webhook (OpenRouter's "Broadcast").

Events carry metadata only (ids, model, tokens, cost, latency), never prompts or completions. Delivery is
best-effort through a bounded queue so a slow receiver can never slow the inference path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("llmgate.broadcast")


class Broadcaster:
    def __init__(self, url: str, client: httpx.AsyncClient | None = None, queue_size: int = 1000):
        self.url, self.client = url, client
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(queue_size)
        self.dropped = 0
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def emit(self, event: dict[str, Any]) -> None:
        if not self.enabled or self.client is None:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1

    async def _run(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self.client.post(self.url, json=event, timeout=5)  # type: ignore[union-attr]
            except httpx.HTTPError as exc:
                log.warning("usage webhook failed: %s", type(exc).__name__)

    def start(self) -> None:
        if self.enabled and self.client is not None:
            self._task = asyncio.create_task(self._run(), name="llmgate-broadcast")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
