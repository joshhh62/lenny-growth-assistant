"""Server-Sent Events protocol between the agent and the UI.

Event types (field `type`):
  provider   {provider, model, runtime, fallback_used, reason}
  status     {text}                              # "Searching transcripts…"
  token      {text}                              # streamed answer delta
  tool_call  {name, input}                       # for the activity trace
  tool_result{name, summary}
  citations  {items: [Citation]}                 # numbered in prompt order
  artifact   {id, kind, title}                   # content fetched via /artifacts/{id}
  done       {message_id, latency_ms, usage}
  error      {code, message, retryable}
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator


class EventStream:
    """Async queue of events; the HTTP layer drains it as SSE."""

    def __init__(self) -> None:
        self._q: asyncio.Queue[dict | None] = asyncio.Queue()

    async def emit(self, type_: str, **data: Any) -> None:
        await self._q.put({"type": type_, **data})

    async def close(self) -> None:
        await self._q.put(None)

    async def __aiter__(self) -> AsyncIterator[dict]:
        while True:
            item = await self._q.get()
            if item is None:
                return
            yield item


def sse_format(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"
