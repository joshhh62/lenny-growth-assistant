"""Agent runtimes.

Two implementations of one small interface:

  MessagesLoopRuntime — a lean tool-calling loop on Ollama's Anthropic-compatible
      Messages endpoint. Used for local models: minimal prompt overhead,
      predictable latency on CPU.

  GeminiRuntime — the same loop on Google's Gemini API, streaming over SSE with
      our tools exposed as Gemini function declarations. Used for the cloud provider.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
import httpx

from app.agent.tools import ALL_TOOLS, ToolContext, dispatch
from app.config import get_settings
from app.llm.providers import make_ollama_client
from app.logging_setup import get_logger

log = get_logger("agent")


@dataclass
class RunResult:
    text: str
    usage: dict = field(default_factory=dict)
    rounds: int = 0
    stop_reason: str | None = None


class AgentError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


class Runtime(Protocol):
    name: str

    async def chat(self, *, system: str, messages: list[dict], ctx: ToolContext, use_tools: bool = True) -> RunResult: ...

    async def generate(self, system: str, user: str, *, max_tokens: int | None = None) -> str: ...


def _map_api_error(exc: Exception, provider: str) -> AgentError:
    if isinstance(exc, anthropic.AuthenticationError):
        return AgentError("auth", f"The {provider} endpoint rejected the request credentials.", False)
    if isinstance(exc, anthropic.APITimeoutError) or isinstance(exc, asyncio.TimeoutError):
        return AgentError("timeout", f"The {provider} model timed out. Try a shorter question or a smaller model.", True)
    if isinstance(exc, anthropic.APIConnectionError):
        return AgentError("unreachable", f"Could not reach the {provider} model endpoint.", True)
    if isinstance(exc, anthropic.RateLimitError):
        return AgentError("rate_limited", "The provider is rate limiting requests. Please retry shortly.", True)
    if isinstance(exc, anthropic.NotFoundError):
        return AgentError("model_not_found", f"Model not found on {provider}. Pull it or fix the model name.", False)
    if isinstance(exc, anthropic.APIStatusError):
        return AgentError("provider_error", f"{provider} error {exc.status_code}: {str(exc)[:200]}", exc.status_code >= 500)
    return AgentError("unknown", f"{type(exc).__name__}: {str(exc)[:200]}", False)


# ---------------------------------------------------------------------------
# Messages loop
# ---------------------------------------------------------------------------
class MessagesLoopRuntime:
    name = "messages_loop"

    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.model = model
        self.client = make_ollama_client()
        self.settings = get_settings()

    async def generate(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        try:
            resp = await self.client.messages.create(
                model=self.model,
                system=system,
                max_tokens=max_tokens or self.settings.llm_max_output_tokens,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # noqa: BLE001
            raise _map_api_error(exc, self.provider) from exc
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    async def chat(self, *, system: str, messages: list[dict], ctx: ToolContext, use_tools: bool = True) -> RunResult:
        s = self.settings
        convo: list[dict] = [dict(m) for m in messages]
        tools = [t.anthropic_schema() for t in ALL_TOOLS] if use_tools else []
        text_out: list[str] = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        rounds = 0
        stop = None

        while rounds < s.llm_max_tool_rounds:
            rounds += 1
            kwargs: dict[str, Any] = dict(
                model=self.model, system=system, messages=convo,
                max_tokens=s.max_output_tokens_for(self.provider),  # type: ignore[arg-type]
            )
            if tools:
                kwargs["tools"] = tools
            try:
                async with self.client.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        et = getattr(event, "type", "")
                        if et == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                            text_out.append(event.delta.text)
                            await ctx.events.emit("token", text=event.delta.text)
                    final = await stream.get_final_message()
            except Exception as exc:  # noqa: BLE001
                raise _map_api_error(exc, self.provider) from exc

            u = getattr(final, "usage", None)
            if u:
                usage["input_tokens"] += getattr(u, "input_tokens", 0) or 0
                usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
            stop = final.stop_reason
            tool_uses = [b for b in final.content if getattr(b, "type", "") == "tool_use"]
            if not tool_uses:
                break

            convo.append({"role": "assistant", "content": [_block_to_dict(b) for b in final.content]})
            results = []
            for tu in tool_uses:
                args = tu.input if isinstance(tu.input, dict) else _parse_args(tu.input)
                out = await dispatch(ctx, tu.name, args)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
            convo.append({"role": "user", "content": results})
            if text_out and text_out[-1] and not text_out[-1].endswith("\n"):
                await ctx.events.emit("token", text="\n\n")
                text_out.append("\n\n")

        return RunResult("".join(text_out).strip(), usage, rounds, stop)


def _block_to_dict(b: Any) -> dict:
    t = getattr(b, "type", "")
    if t == "text":
        return {"type": "text", "text": b.text}
    if t == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return {"type": t}


def _parse_args(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"query": raw}
    return dict(raw or {})


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
_GEMINI_RETRYABLE = {429, 500, 503}
_GEMINI_RETRIES = 2


class GeminiHTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _gemini_error_message(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error", {}).get("message", "")) or r.text[:300]
    except ValueError:
        return r.text[:300]


def _map_gemini_error(exc: Exception, model: str) -> AgentError:
    if isinstance(exc, AgentError):
        return exc
    if isinstance(exc, GeminiHTTPError):
        code, msg = exc.status_code, str(exc)
        if code in (401, 403) or (code == 400 and "api key" in msg.lower()):
            return AgentError("auth", "Gemini rejected the API key. Check GEMINI_API_KEY.", False)
        if code == 404:
            return AgentError("model_not_found", f"Model '{model}' not found on Gemini. Check GEMINI_MODEL.", False)
        if code == 429:
            return AgentError("rate_limited", "Gemini is rate limiting requests. Please retry shortly.", True)
        return AgentError("provider_error", f"gemini error {code}: {msg[:200]}", code >= 500)
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return AgentError("timeout", "The gemini model timed out. Try a shorter question.", True)
    if isinstance(exc, httpx.TransportError):
        return AgentError("unreachable", "Could not reach the Gemini API.", True)
    return AgentError("unknown", f"{type(exc).__name__}: {str(exc)[:200]}", False)


def _to_gemini_contents(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        out.append({"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": str(content)}]})
    return out


def _check_blocked(data: dict) -> None:
    reason = (data.get("promptFeedback") or {}).get("blockReason")
    if reason and not data.get("candidates"):
        raise AgentError("blocked", f"Gemini declined to answer this prompt ({reason}).", False)


class GeminiRuntime:
    """Tool-calling loop on the Gemini API (`generateContent` / `streamGenerateContent`).

    Function-call parts are replayed verbatim (including `thoughtSignature`),
    which Gemini 3 models require to continue a tool-calling turn."""

    name = "gemini_api"

    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.model = model
        self.settings = get_settings()

    def _client(self) -> httpx.AsyncClient:
        s = self.settings
        return httpx.AsyncClient(
            timeout=httpx.Timeout(s.llm_timeout_s, connect=10.0),
            headers={"x-goog-api-key": s.gemini_api_key.strip()},
        )

    def _body(self, system: str, contents: list[dict], max_tokens: int, tools: list[dict] | None = None) -> dict:
        gen: dict[str, Any] = {"maxOutputTokens": max_tokens}
        if self.settings.gemini_thinking_level:
            gen["thinkingConfig"] = {"thinkingLevel": self.settings.gemini_thinking_level}
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": gen,
        }
        if tools:
            body["tools"] = [{"functionDeclarations": tools}]
        return body

    @asynccontextmanager
    async def _request(self, client: httpx.AsyncClient, method: str, body: dict):
        """POST to models/<model>:<method>, retrying overload/rate-limit responses
        before any output has been consumed."""
        url = f"{self.settings.gemini_base_url.rstrip('/')}/models/{self.model}:{method}"
        for attempt in range(_GEMINI_RETRIES + 1):
            r = await client.send(client.build_request("POST", url, json=body), stream=True)
            try:
                if r.status_code in _GEMINI_RETRYABLE and attempt < _GEMINI_RETRIES:
                    await r.aread()
                    log.warning("gemini_retry", status=r.status_code, attempt=attempt + 1)
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    await r.aread()
                    raise GeminiHTTPError(r.status_code, _gemini_error_message(r))
                yield r
                return
            finally:
                await r.aclose()

    async def generate(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        body = self._body(system, [{"role": "user", "parts": [{"text": user}]}],
                          max_tokens or self.settings.llm_max_output_tokens)
        try:
            async with self._client() as client, self._request(client, "generateContent", body) as r:
                data = json.loads(await r.aread())
            _check_blocked(data)
        except Exception as exc:  # noqa: BLE001
            raise _map_gemini_error(exc, self.model) from exc
        parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    async def chat(self, *, system: str, messages: list[dict], ctx: ToolContext, use_tools: bool = True) -> RunResult:
        s = self.settings
        convo = _to_gemini_contents(messages)
        tools = [t.gemini_schema() for t in ALL_TOOLS] if use_tools else []
        text_out: list[str] = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        rounds = 0
        stop = None

        while rounds < s.llm_max_tool_rounds:
            rounds += 1
            body = self._body(system, convo, s.max_output_tokens_for(self.provider), tools)  # type: ignore[arg-type]
            parts: list[dict] = []
            calls: list[dict] = []
            meta: dict = {}
            try:
                async with self._client() as client, \
                        self._request(client, "streamGenerateContent?alt=sse", body) as r:
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        chunk = json.loads(line[5:].strip() or "{}")
                        _check_blocked(chunk)
                        meta = chunk.get("usageMetadata") or meta
                        cand = (chunk.get("candidates") or [{}])[0]
                        stop = cand.get("finishReason") or stop
                        for p in (cand.get("content") or {}).get("parts") or []:
                            if p.keys() == {"text"} and not p["text"]:
                                continue  # empty trailing part; Gemini rejects these on replay
                            parts.append(p)
                            if "functionCall" in p:
                                calls.append(p["functionCall"])
                            elif p.get("text") and not p.get("thought"):
                                text_out.append(p["text"])
                                await ctx.events.emit("token", text=p["text"])
            except Exception as exc:  # noqa: BLE001
                raise _map_gemini_error(exc, self.model) from exc

            usage["input_tokens"] += meta.get("promptTokenCount", 0) or 0
            usage["output_tokens"] += meta.get("candidatesTokenCount", 0) or 0
            if not calls:
                break

            convo.append({"role": "model", "parts": parts})
            responses = []
            for fc in calls:
                out = await dispatch(ctx, fc["name"], dict(fc.get("args") or {}))
                fr: dict[str, Any] = {"name": fc["name"], "response": {"result": out}}
                if fc.get("id"):
                    fr["id"] = fc["id"]
                responses.append({"functionResponse": fr})
            convo.append({"role": "user", "parts": responses})
            if text_out and text_out[-1] and not text_out[-1].endswith("\n"):
                await ctx.events.emit("token", text="\n\n")
                text_out.append("\n\n")

        return RunResult("".join(text_out).strip(), usage, rounds, stop)


def build_runtime(provider: str, model: str, runtime_name: str) -> Runtime:
    if runtime_name == "gemini_api":
        return GeminiRuntime(provider, model)
    return MessagesLoopRuntime(provider, model)
