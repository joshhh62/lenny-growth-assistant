"""Chat orchestration: one user turn in, a stream of events out.

    resolve provider → persist user msg → route intent → (retrieve) → run runtime/skill
    → post-process citations → persist assistant msg → done

Every failure mode the brief lists (missing key, Ollama down, timeout, empty
retrieval, DB down) is surfaced as a typed `error` event AND recorded on the
assistant message so the conversation history explains itself later.
"""

from __future__ import annotations

import re
import time
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.events import EventStream
from app.agent.prompts import ARTIFACT_HINT, SYSTEM_PROMPT, format_passages
from app.agent.router import Intent, Route, route
from app.agent.runtime import AgentError, RunResult, build_runtime
from app.agent.tools import ToolContext, _store_artifact, dispatch
from app.config import get_settings
from app.db.repository import ConversationRepo, KnowledgeRepo
from app.llm.providers import NoProviderAvailable, resolve
from app.logging_setup import get_logger, session_id_var
from app.rag.embeddings import OllamaEmbedder
from app.rag.ingest import estimate_tokens
from app.rag.retriever import Retriever

log = get_logger("chat")

_REFERENTIAL = re.compile(r"^(that|this|it|the above|the previous|previous answer|last answer|your answer)\b", re.I)
_FENCE = re.compile(r"```(?:html|markdown|md)?\s*\n(.*?)```", re.S | re.I)

ARTIFACT_SYSTEM = (
    "You produce a single self-contained artifact grounded ONLY in the supplied transcript passages. "
    "Output the artifact and nothing else — no preamble, no explanation, no code fences."
)


class ChatService:
    def __init__(self, db: AsyncSession, embedder: OllamaEmbedder | None):
        self.db = db
        self.convo = ConversationRepo(db)
        self.retriever = Retriever(KnowledgeRepo(db), embedder)
        self.settings = get_settings()

    async def handle(self, session: dict, user_text: str, events: EventStream) -> None:
        sid: uuid.UUID = session["id"]
        session_id_var.set(str(sid))
        t0 = time.perf_counter()
        user_text = user_text.strip()

        # 1. Provider resolution (documented fallback behaviour).
        try:
            status, fallback_used = await resolve(session.get("provider"))
        except NoProviderAvailable as exc:
            await self._fail(sid, user_text, events, "no_provider", str(exc), retryable=True, t0=t0)
            return
        runtime_name = self.settings.runtime_for(status.name)  # type: ignore[arg-type]
        await events.emit(
            "provider", provider=status.name, model=status.model, runtime=runtime_name,
            fallback_used=fallback_used, reason=("" if not fallback_used else f"{session.get('provider')} unavailable"),
        )
        runtime = build_runtime(status.name, status.model, runtime_name)

        # 2. Persist the user turn, auto-title the session on first message.
        history = await self.convo.list_messages(sid)
        await self.convo.add_message(sid, "user", user_text)
        if not history and session.get("title", "New chat") == "New chat":
            await self.convo.update_session(sid, title=user_text[:60])
        await self.db.commit()

        ctx = ToolContext(session_id=sid, retriever=self.retriever, convo=self.convo, events=events,
                          generate=runtime.generate, cheap_generation=(status.name == "ollama"))
        decision = route(user_text)
        if decision.intent != Intent.CHAT and _REFERENTIAL.match(decision.topic):
            decision = Route(decision.intent, self._last_user_topic(history) or decision.topic, decision.angle, decision.reason)
        log.info("routed", intent=decision.intent.value, topic=decision.topic[:80], runtime=runtime_name,
                 provider=status.name)

        try:
            if decision.intent == Intent.ESSAY:
                result = await self._essay(ctx, runtime, decision)
            elif decision.intent in (Intent.ARTIFACT_MARKDOWN, Intent.ARTIFACT_HTML):
                result = await self._artifact(ctx, runtime, decision, history, user_text)
            else:
                result = await self._chat(ctx, runtime, history, user_text)
        except AgentError as exc:
            await self._fail(sid, user_text, events, exc.code, exc.message, exc.retryable, t0=t0,
                             provider=status.name, model=status.model, runtime=runtime_name, persisted_user=True)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("turn_failed")
            await self._fail(sid, user_text, events, "internal", f"Unexpected error: {type(exc).__name__}",
                             True, t0=t0, provider=status.name, model=status.model, runtime=runtime_name, persisted_user=True)
            return

        # 3. Keep only citations the answer actually references (fallback: top 3 consulted).
        referenced = {int(n) for n in re.findall(r"\[(\d+)\]", result.text)}
        numbered = [c.to_dict() | {"n": i} for i, c in enumerate(ctx.citations, 1)]
        used = [c for c in numbered if c["n"] in referenced] or numbered[:3]
        if used:
            await events.emit("citations", items=used, final=True)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        msg = await self.convo.add_message(
            sid, "assistant", result.text or "(no answer produced)",
            citations=used, tool_calls=ctx.tool_calls, provider=status.name, model=status.model,
            runtime=runtime_name, latency_ms=latency_ms,
            input_tokens=result.usage.get("input_tokens"), output_tokens=result.usage.get("output_tokens"),
        )
        if ctx.artifacts:
            # Link artifacts created during this turn to the assistant message.
            from sqlalchemy import text as _t
            await self.db.execute(
                _t("UPDATE artifacts SET message_id = :mid WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"mid": msg["id"], "ids": [a["id"] for a in ctx.artifacts]},
            )
        await self.db.commit()
        log.info("turn_complete", latency_ms=latency_ms, intent=decision.intent.value,
                 citations=len(used), artifacts=len(ctx.artifacts), rounds=result.rounds, **result.usage)
        await events.emit("done", message_id=str(msg["id"]), latency_ms=latency_ms, usage=result.usage,
                          artifacts=[a["id"] for a in ctx.artifacts])

    # ------------------------------------------------------------------ chat
    async def _chat(self, ctx: ToolContext, runtime, history: list[dict], user_text: str) -> RunResult:
        # Retrieve-then-generate: pre-fetch passages for the new turn so even a
        # small model that never calls tools is grounded. Follow-ups that lean on
        # pronouns are expanded with the previous user question.
        query = user_text
        if len(user_text.split()) <= 6 or _REFERENTIAL.match(user_text):
            prev = self._last_user_topic(history)
            if prev:
                query = f"{prev} {user_text}"
        await ctx.events.emit("status", text="Searching transcripts…")
        found, _ = await self.retriever.search(query, top_k=self.settings.retrieval_top_k_for(runtime.provider))  # type: ignore[arg-type]
        ctx.add_citations(found)
        numbered = [c.to_dict() | {"n": i} for i, c in enumerate(ctx.citations, 1)]
        await ctx.events.emit("citations", items=numbered)

        context_block = format_passages([c.to_dict() for c in ctx.citations])
        messages = self._history_messages(history, self.settings.history_budget_for(runtime.provider)) + [  # type: ignore[arg-type]
            {"role": "user", "content": f"{user_text}\n\n{context_block}"}
        ]
        await ctx.events.emit("status", text="Thinking…")
        use_tools = self.settings.tools_enabled_for(runtime.provider)  # type: ignore[arg-type]
        return await runtime.chat(system=SYSTEM_PROMPT, messages=messages, ctx=ctx, use_tools=use_tools)

    # ----------------------------------------------------------------- essay
    async def _essay(self, ctx: ToolContext, runtime, decision: Route) -> RunResult:
        outcome = await dispatch(ctx, "write_ship30_essay", {"topic": decision.topic, "angle": decision.angle})
        if outcome.startswith("Could not write") or outcome.startswith("Error"):
            await ctx.events.emit("token", text=outcome)
            return RunResult(outcome)
        art = ctx.artifacts[-1] if ctx.artifacts else None
        preview = await self._artifact_preview(art) if art else ""
        summary_prompt = [{
            "role": "user",
            "content": (
                f"I asked for a Ship 30 for 30 essay on '{decision.topic}'. It has been created and is open in the "
                f"artifact viewer. Here is the beginning:\n\n{preview}\n\nIn 2-3 sentences tell me the essay's hook "
                "and its single most useful takeaway, citing passages as [n]. Do not repeat the essay."
            ),
        }]
        await ctx.events.emit("status", text="Summarising…")
        return await runtime.chat(system=SYSTEM_PROMPT, messages=summary_prompt, ctx=ctx, use_tools=False)

    # -------------------------------------------------------------- artifact
    async def _artifact(self, ctx: ToolContext, runtime, decision: Route, history: list[dict], user_text: str) -> RunResult:
        kind = "html" if decision.intent == Intent.ARTIFACT_HTML else "markdown"
        await ctx.events.emit("status", text="Gathering transcript passages…")
        found, _ = await self.retriever.search(decision.topic, top_k=8)
        ctx.add_citations(found)
        numbered = [c.to_dict() | {"n": i} for i, c in enumerate(ctx.citations, 1)]
        await ctx.events.emit("citations", items=numbered)
        if not found and not history:
            msg = "The transcripts I have don't cover that topic, so I can't build a grounded artifact."
            await ctx.events.emit("token", text=msg)
            return RunResult(msg)

        recent = "\n\n".join(f"{m['role'].upper()}: {m['content'][:1500]}" for m in history[-4:])
        spec = (
            f"REQUEST: {user_text}\n\nRECENT CONVERSATION (for context):\n{recent or '(none)'}\n\n"
            f"{format_passages([c.to_dict() for c in ctx.citations])}\n\n"
            + ("Produce a COMPLETE HTML document: <!doctype html>, <head> with an inline <style> block, "
               "a clean readable layout (system font stack, max-width 760px, generous spacing, one accent colour). "
               "No JavaScript, no external URLs. Keep [n] citations where claims are made.\n"
               if kind == "html" else
               "Produce a well-structured Markdown document with a # title, ## sections, bullets and a short "
               "Sources section listing the cited passage numbers with guest and episode. Keep [n] citations.\n")
            + ARTIFACT_HINT.split("\n")[0]
        )
        await ctx.events.emit("status", text=f"Drafting {kind} artifact…")
        draft = await runtime.generate(
            ARTIFACT_SYSTEM, spec,
            max_tokens=self.settings.artifact_tokens_for(runtime.provider),  # type: ignore[arg-type]
        )
        m = _FENCE.search(draft)
        content = m.group(1) if m else draft
        if kind == "html" and "<html" not in content.lower():
            content = f"<!doctype html><html><head><meta charset='utf-8'></head><body>{content}</body></html>"
        title = self._title_from(content, kind) or decision.topic[:80] or "Artifact"
        await _store_artifact(ctx, kind, title, content)
        ctx.tool_calls.append({"name": "create_artifact", "input": {"kind": kind, "title": title}})

        summary_prompt = [{
            "role": "user",
            "content": (
                f"A {kind} artifact titled '{title}' was just created from the transcripts and is open in the "
                f"artifact viewer. It begins:\n\n{content[:1200]}\n\nIn 2-3 sentences, tell me what it contains "
                "and which guests it draws on, citing [n]. Do not repeat the artifact."
            ),
        }]
        await ctx.events.emit("status", text="Summarising…")
        return await runtime.chat(system=SYSTEM_PROMPT, messages=summary_prompt, ctx=ctx, use_tools=False)

    # --------------------------------------------------------------- helpers
    def _history_messages(self, history: list[dict], budget_tokens: int = 2200) -> list[dict]:
        """Most recent turns that fit the token budget, alternating roles."""
        out: list[dict] = []
        used = 0
        for m in reversed(history):
            if m["role"] not in ("user", "assistant") or m.get("error"):
                continue
            t = estimate_tokens(m["content"])
            if used + t > budget_tokens and out:
                break
            out.append({"role": m["role"], "content": m["content"]})
            used += t
        out.reverse()
        # The Messages API requires the first message to be from the user.
        while out and out[0]["role"] != "user":
            out.pop(0)
        return out

    @staticmethod
    def _last_user_topic(history: list[dict]) -> str | None:
        for m in reversed(history):
            if m["role"] == "user":
                return m["content"][:300]
        return None

    @staticmethod
    def _title_from(content: str, kind: str) -> str | None:
        if kind == "html":
            m = re.search(r"<title>(.*?)</title>", content, re.I | re.S) or re.search(r"<h1[^>]*>(.*?)</h1>", content, re.I | re.S)
        else:
            m = re.search(r"^#\s+(.+)$", content, re.M)
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()[:120] if m else None

    async def _artifact_preview(self, art: dict) -> str:
        row = await self.convo.get_artifact(uuid.UUID(art["id"]))
        return (row["content"][:1500] if row else "")

    async def _fail(self, sid, user_text, events, code, message, retryable, *, t0, provider=None, model=None,
                    runtime=None, persisted_user=False) -> None:
        log.warning("turn_error", code=code, message=message)
        try:
            if not persisted_user:
                await self.convo.add_message(sid, "user", user_text)
            await self.convo.add_message(
                sid, "assistant", f"⚠️ {message}", provider=provider, model=model, runtime=runtime,
                latency_ms=int((time.perf_counter() - t0) * 1000), error=code,
            )
            await self.db.commit()
        except Exception:  # noqa: BLE001 — DB may be the thing that failed
            log.exception("persist_error_failed")
        await events.emit("error", code=code, message=message, retryable=retryable)
