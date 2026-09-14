"""Agent runtimes.

Two implementations of one small interface:

  MessagesLoopRuntime — a lean tool-calling loop on the Anthropic Messages API.
      Works against Anthropic *and* Ollama's Anthropic-compatible endpoint.
      Default for local models: minimal prompt overhead, predictable latency on CPU.

  AgentSDKRuntime — the Claude Agent SDK (`claude_agent_sdk.query`) with our tools
      exposed as an in-process MCP server. Default for Anthropic: production agent
      loop, hooks, budgets, and the same tool definitions.

The trade-off between them is discussed in architecture.md → "Agent runtimes".
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic

from app.agent.tools import ALL_TOOLS, ToolContext, dispatch
from app.config import get_settings
from app.llm.providers import agent_sdk_env, make_client
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
        return AgentError("auth", "The cloud provider rejected the API key. Check ANTHROPIC_API_KEY.", False)
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
        self.client = make_client(provider)
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
# Claude Agent SDK
# ---------------------------------------------------------------------------
class AgentSDKRuntime:
    name = "agent_sdk"

    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.model = model
        self.settings = get_settings()
        # Sub-generations (essay skill, artifact drafts) use the plain client: they are
        # single-shot completions where an agent loop adds nothing but latency.
        self._plain = MessagesLoopRuntime(provider, model)

    async def generate(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        return await self._plain.generate(system, user, max_tokens=max_tokens)

    async def chat(self, *, system: str, messages: list[dict], ctx: ToolContext, use_tools: bool = True) -> RunResult:
        from claude_agent_sdk import (
            AssistantMessage, ClaudeAgentOptions, ResultMessage, StreamEvent, TextBlock,
            create_sdk_mcp_server, query, tool,
        )

        # Wrap the shared tool handlers as SDK MCP tools bound to this request's context.
        sdk_tools = []
        for spec in ALL_TOOLS:
            def _make(spec_=spec):  # bind per iteration
                @tool(spec_.name, spec_.description, spec_.input_schema)
                async def _t(args: dict) -> dict:
                    out = await dispatch(ctx, spec_.name, args)
                    return {"content": [{"type": "text", "text": out}]}
                return _t
            sdk_tools.append(_make())
        server = create_sdk_mcp_server(name="lenny", version="1.0.0", tools=sdk_tools)

        prompt = _render_prompt(messages)
        options = ClaudeAgentOptions(
            system_prompt=system,
            model=self.model,
            tools=[],  # no built-in file/shell tools: this agent only talks to our MCP tools
            mcp_servers={"lenny": server},
            allowed_tools=[f"mcp__lenny__{t.name}" for t in ALL_TOOLS] if use_tools else [],
            permission_mode="bypassPermissions",
            max_turns=self.settings.llm_max_tool_rounds,
            env=agent_sdk_env(self.provider),
            include_partial_messages=True,
            setting_sources=[],
        )
        text_out: list[str] = []
        usage: dict = {}
        stop = None
        try:
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, StreamEvent):
                    ev = msg.event or {}
                    if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                        delta = ev["delta"]["text"]
                        text_out.append(delta)
                        await ctx.events.emit("token", text=delta)
                elif isinstance(msg, AssistantMessage) and not text_out:
                    # Fallback if partial streaming is unavailable.
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            text_out.append(b.text)
                            await ctx.events.emit("token", text=b.text)
                elif isinstance(msg, ResultMessage):
                    usage = dict(msg.usage or {})
                    stop = msg.subtype
                    if msg.is_error:
                        raise AgentError("agent_sdk", msg.result or "Agent SDK reported an error", True)
        except AgentError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AgentError("agent_sdk", f"Agent SDK failure: {type(exc).__name__}: {str(exc)[:200]}", True) from exc
        return RunResult("".join(text_out).strip(), usage, 1, stop)


def _render_prompt(messages: list[dict]) -> str:
    """The SDK takes a single prompt; earlier turns are rendered inline."""
    parts = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        parts.append(("User" if m["role"] == "user" else "Assistant") + ": " + str(content))
    return "\n\n".join(parts)


def build_runtime(provider: str, model: str, runtime_name: str) -> Runtime:
    if runtime_name == "agent_sdk":
        return AgentSDKRuntime(provider, model)
    return MessagesLoopRuntime(provider, model)
