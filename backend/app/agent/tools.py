"""Tool registry shared by both agent runtimes.

Each tool is defined ONCE (JSON schema + async handler). The Messages-loop
runtime passes the schemas straight to the Anthropic Messages API; the Agent SDK
runtime wraps the same handlers as an in-process MCP server. This is the
"clear skill boundary" the brief asks for: tools are the only way the model can
touch retrieval, the essay skill, or artifacts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.agent.events import EventStream
from app.agent.prompts import format_passages
from app.artifacts.sanitize import sanitize
from app.config import get_settings
from app.db.repository import ConversationRepo
from app.logging_setup import get_logger
from app.rag.retriever import Citation, Retriever

log = get_logger("tools")


@dataclass
class ToolContext:
    """Per-request state the tools read/write."""

    session_id: uuid.UUID
    retriever: Retriever
    convo: ConversationRepo
    events: EventStream
    generate: Callable[..., Awaitable[str]]  # plain (system, user, *, max_tokens) → text, for sub-skills
    cheap_generation: bool = False  # True for CPU-bound local models: fewer/shorter passes
    citations: list[Citation] = field(default_factory=list)  # numbered 1..n in order added
    artifacts: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)

    def add_citations(self, new: list[Citation]) -> list[int]:
        """Append unseen citations; return their 1-based numbers."""
        numbers = []
        known = {c.id: i + 1 for i, c in enumerate(self.citations)}
        for c in new:
            if c.id in known:
                numbers.append(known[c.id])
            else:
                self.citations.append(c)
                known[c.id] = len(self.citations)
                numbers.append(known[c.id])
        return numbers


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[ToolContext, dict], Awaitable[str]]

    def anthropic_schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


# --- search_transcripts -----------------------------------------------------
async def _search_transcripts(ctx: ToolContext, args: dict) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "Error: query is required."
    top_k = min(int(args.get("top_k", get_settings().retrieval_top_k)), 10)
    await ctx.events.emit("status", text=f"Searching transcripts for “{query[:60]}”…")
    found, diag = await ctx.retriever.search(query, top_k=top_k)
    if not found:
        return "No transcript passages matched. Tell the user the transcripts don't cover this."
    numbers = ctx.add_citations(found)
    await ctx.events.emit("citations", items=[c.to_dict() | {"n": n} for c, n in zip(ctx.citations, range(1, len(ctx.citations) + 1))])
    # Present passages with their *global* numbers so citations stay stable across calls.
    numbered = [c.to_dict() for c in found]
    text = format_passages(numbered)
    # format_passages numbers from 1; remap to the global numbers.
    for local, global_n in enumerate(numbers, 1):
        text = text.replace(f"[{local}] Episode:", f"[{global_n}] Episode:", 1)
    return text


SEARCH_TOOL = ToolSpec(
    name="search_transcripts",
    description=(
        "Search Lenny's Podcast transcripts for passages relevant to a product/growth question. "
        "Returns numbered passages to cite as [n]. Use specific queries (e.g. 'activation metric "
        "definition' rather than 'metrics')."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Specific search query."},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 6},
        },
        "required": ["query"],
    },
    handler=_search_transcripts,
)


# --- write_ship30_essay -----------------------------------------------------
async def _write_essay(ctx: ToolContext, args: dict) -> str:
    from app.skills.ship30.essay import write_essay

    topic = str(args.get("topic", "")).strip()
    angle = str(args.get("angle", "actionable")).lower()
    if not topic:
        return "Error: topic is required."
    try:
        essay, cites, check = await write_essay(
            topic, angle, ctx.retriever, ctx.generate,
            on_status=lambda t: ctx.events.emit("status", text=t),
            cheap_revision=ctx.cheap_generation,
        )
    except LookupError as exc:
        return f"Could not write the essay: {exc}"
    ctx.add_citations(cites)
    await ctx.events.emit("citations", items=[c.to_dict() | {"n": i} for i, c in enumerate(ctx.citations, 1)])
    title = essay.splitlines()[0].lstrip("# ").strip() or f"Essay: {topic}"
    art = await _store_artifact(ctx, "markdown", title, essay)
    return (
        f"Essay created as artifact '{title}' ({check.word_count} words, {check.citations} citations; "
        f"checks {'passed' if check.ok else 'partially passed: ' + '; '.join(check.problems)}). "
        f"It is now displayed in the artifact viewer. Summarise its hook and takeaway for the user in 2-3 sentences."
    )


ESSAY_TOOL = ToolSpec(
    name="write_ship30_essay",
    description=(
        "Write a ~1,250-word Ship 30 for 30 style essay on a product/growth topic, grounded in the "
        "transcripts, and open it in the artifact viewer. Use when the user asks for an essay, article, "
        "newsletter piece or long-form post."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Specific essay topic."},
            "angle": {
                "type": "string",
                "enum": ["actionable", "analytical", "aspirational", "anthropological"],
                "description": "Ship 30 '4A' angle. Default actionable.",
            },
        },
        "required": ["topic"],
    },
    handler=_write_essay,
)


# --- create_artifact --------------------------------------------------------
async def _store_artifact(ctx: ToolContext, kind: str, title: str, content: str) -> dict:
    s = get_settings()
    clean, report = sanitize(kind, content, s.artifact_max_bytes)
    if report.removed_tags or report.stripped_urls or report.removed_css_rules:
        log.warning("artifact_sanitized", kind=kind, **report.to_dict())
    row = await ctx.convo.add_artifact(ctx.session_id, None, kind, title[:200], clean)
    # Commit now: the UI fetches the artifact as soon as it sees the event, before the turn ends.
    await ctx.convo.db.commit()
    art = {"id": str(row["id"]), "kind": kind, "title": title[:200], "sanitize_report": report.to_dict()}
    ctx.artifacts.append(art)
    await ctx.events.emit("artifact", **art)
    return art


async def _create_artifact(ctx: ToolContext, args: dict) -> str:
    kind = str(args.get("kind", "markdown")).lower()
    if kind not in ("markdown", "html"):
        return "Error: kind must be 'markdown' or 'html'."
    title = str(args.get("title", "")).strip() or "Untitled artifact"
    content = str(args.get("content", ""))
    if len(content) < 20:
        return "Error: content is too short; provide the full document."
    await ctx.events.emit("status", text=f"Rendering {kind} artifact “{title[:50]}”…")
    art = await _store_artifact(ctx, kind, title, content)
    note = ""
    if art["sanitize_report"]["removed_tags"]:
        note = f" (removed disallowed elements: {', '.join(art['sanitize_report']['removed_tags'])})"
    return f"Artifact '{title}' rendered in the viewer{note}. Give the user a one-paragraph summary."


ARTIFACT_TOOL = ToolSpec(
    name="create_artifact",
    description=(
        "Create a rendered artifact beside the chat: a Markdown document or a complete, self-contained "
        "HTML/CSS page (inline <style>, no scripts, no external resources). Use when the user asks for a "
        "document, page, checklist, table, one-pager, dashboard mock, or similar."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["markdown", "html"]},
            "title": {"type": "string"},
            "content": {"type": "string", "description": "Full Markdown or full HTML document."},
        },
        "required": ["kind", "title", "content"],
    },
    handler=_create_artifact,
)

ALL_TOOLS: list[ToolSpec] = [SEARCH_TOOL, ESSAY_TOOL, ARTIFACT_TOOL]
TOOLS_BY_NAME = {t.name: t for t in ALL_TOOLS}


async def dispatch(ctx: ToolContext, name: str, args: dict) -> str:
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        return f"Error: unknown tool '{name}'."
    await ctx.events.emit("tool_call", name=name, input={k: (v if len(str(v)) < 200 else str(v)[:200] + "…") for k, v in args.items()})
    ctx.tool_calls.append({"name": name, "input": {k: str(v)[:500] for k, v in args.items()}})
    try:
        result = await spec.handler(ctx, args)
    except Exception as exc:  # noqa: BLE001 — a tool failure must not kill the turn
        log.exception("tool_failed", tool=name)
        result = f"Error: tool '{name}' failed: {type(exc).__name__}: {str(exc)[:200]}"
    log.info("tool_result", tool=name, summary=result[:160])
    await ctx.events.emit("tool_result", name=name, summary=result[:160])
    return result
