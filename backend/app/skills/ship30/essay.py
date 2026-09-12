"""Ship 30 for 30 essay pipeline.

    topic ──► query expansion ──► hybrid retrieval (3 queries) ──► generation with SKILL.md
          ──► programmatic checks ──► (one revision pass if checks fail) ──► essay + citations

The skill lives in SKILL.md; this module only orchestrates and validates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from app.agent.prompts import format_passages
from app.logging_setup import get_logger
from app.rag.retriever import Citation, Retriever

log = get_logger("skill.ship30")

SKILL_PATH = Path(__file__).parent / "SKILL.md"
ANGLES = ("actionable", "analytical", "aspirational", "anthropological")


def load_skill() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


@dataclass
class EssayCheck:
    ok: bool
    word_count: int
    h1: int
    h2: int
    citations: int
    bad_citations: list[int]
    bullets: int
    bolds: int
    has_next_steps: bool
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__


def check_essay(md: str, n_passages: int) -> EssayCheck:
    body = re.split(r"^###?\s*Sources\s*$", md, flags=re.M | re.I)[0]
    words = len(re.findall(r"\b\w+\b", re.sub(r"\[\d+\]", "", body)))
    h1 = len(re.findall(r"^# ", body, flags=re.M))
    h2 = len(re.findall(r"^## ", body, flags=re.M))
    cites = [int(x) for x in re.findall(r"\[(\d+)\]", body)]
    bad = sorted({c for c in cites if c < 1 or c > n_passages})
    bullets = len(re.findall(r"^\s*[-*] ", body, flags=re.M))
    bolds = len(re.findall(r"\*\*[^*\n]+\*\*", body))
    next_steps = bool(re.search(r"^## .*(do this next|next step|takeaway)", body, flags=re.M | re.I))
    problems = []
    if not 1000 <= words <= 1500:
        problems.append(f"word count {words} outside 1000-1500")
    if h1 != 1:
        problems.append(f"expected 1 H1, found {h1}")
    if not 3 <= h2 <= 7:
        problems.append(f"expected 3-7 H2 sections, found {h2}")
    if len(cites) < 5:
        problems.append(f"only {len(cites)} citations")
    if bad:
        problems.append(f"citations reference unknown passages: {bad}")
    if bullets < 4:
        problems.append(f"only {bullets} bullets")
    if bolds < 2:
        problems.append(f"only {bolds} bold phrases")
    if not next_steps:
        problems.append("missing 'Do this next' section")
    if re.search(r"in today'?s|in this essay|as we all know", body[:400], flags=re.I):
        problems.append("weak opener")
    return EssayCheck(not problems, words, h1, h2, len(cites), bad, bullets, bolds, next_steps, problems)


def _expand_queries(topic: str, angle: str) -> list[str]:
    base = topic.strip()
    variants = {
        "actionable": [base, f"how to {base}", f"{base} tactics framework"],
        "analytical": [base, f"{base} metrics data", f"why {base} works"],
        "aspirational": [base, f"{base} story example", f"{base} success"],
        "anthropological": [base, f"why people {base}", f"{base} psychology behaviour"],
    }
    return variants.get(angle, variants["actionable"])


def sources_block(citations: list[Citation]) -> str:
    lines = ["### Sources"]
    for i, c in enumerate(citations, 1):
        ts = c.timestamp_url or c.youtube_url or ""
        lines.append(f"[{i}] {c.guest} — {c.title}" + (f" ([{_fmt(c.start_seconds)}]({ts}))" if ts else ""))
    return "\n".join(lines)


def _fmt(seconds: int | None) -> str:
    if seconds is None:
        return "link"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


Generate = Callable[..., Awaitable[str]]  # (system, user, *, max_tokens) -> markdown
ESSAY_MAX_TOKENS = 2200  # ~1,400 words + headings


async def write_essay(
    topic: str,
    angle: str,
    retriever: Retriever,
    generate: Generate,
    *,
    on_status: Callable[[str], Awaitable[None]] | None = None,
    cheap_revision: bool = False,
) -> tuple[str, list[Citation], EssayCheck]:
    angle = angle if angle in ANGLES else "actionable"
    if on_status:
        await on_status("Gathering transcript passages for the essay…")

    # Multi-query retrieval, merged and deduped, capped at 8–10 passages (~3k tokens).
    seen: set[int] = set()
    citations: list[Citation] = []
    for q in _expand_queries(topic, angle):
        found, _ = await retriever.search(q, top_k=5)
        for c in found:
            if c.id not in seen:
                seen.add(c.id)
                citations.append(c)
    citations = citations[: 8 if cheap_revision else 10]  # keep local prompts inside an 8k window
    if len(citations) < 2:
        raise LookupError(
            f"Not enough transcript material about '{topic}' to write a grounded essay."
        )

    passages = format_passages([c.to_dict() for c in citations])
    system = (
        "You are a writing engine. Follow the SKILL exactly. Output Markdown only — no preamble.\n\n"
        + load_skill()
    )
    user = (
        f"TOPIC: {topic}\nANGLE: {angle}\n\n{passages}\n\n"
        "Write the essay now. Cite passages by number. Do not include a Sources section; it is appended automatically."
    )
    if on_status:
        await on_status("Drafting the essay (Ship 30 for 30 skill)…")
    draft = await generate(system, user, max_tokens=ESSAY_MAX_TOKENS)
    check = check_essay(draft, len(citations))
    log.info("essay_draft", **{k: v for k, v in check.to_dict().items() if k != "problems"}, problems=check.problems)

    # A revision pass costs a full second generation (minutes on a CPU model), so
    # locally we only revise for severe failures; cloud revises for any failure.
    severe = check.word_count < 800 or check.citations < 3 or bool(check.bad_citations) or check.h2 < 2
    if not check.ok and (severe or not cheap_revision):
        if on_status:
            await on_status("Revising to meet the skill checklist…")
        revision_user = (
            user
            + "\n\nYour previous draft failed these checks:\n- "
            + "\n- ".join(check.problems)
            + "\n\nRewrite the full essay fixing every point. Output Markdown only."
        )
        revised = await generate(system, revision_user, max_tokens=ESSAY_MAX_TOKENS)
        check2 = check_essay(revised, len(citations))
        if len(check2.problems) <= len(check.problems):
            draft, check = revised, check2
        log.info("essay_revised", problems=check.problems)

    draft = re.split(r"^###?\s*Sources\s*$", draft, flags=re.M | re.I)[0].rstrip()
    essay = f"{draft}\n\n{sources_block(citations)}\n"
    return essay, citations, check
