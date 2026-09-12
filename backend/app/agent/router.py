"""Intent router.

Reliability principle: *skills* (essay, artifact) are routed deterministically —
we never rely on a 7B local model remembering to call the right tool with a
2,000-word argument. Free-form questions go to the model, which may call
`search_transcripts` itself for follow-ups. The router is pure and unit-tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Intent(str, Enum):
    CHAT = "chat"
    ESSAY = "essay"
    ARTIFACT_MARKDOWN = "artifact_markdown"
    ARTIFACT_HTML = "artifact_html"


@dataclass(frozen=True)
class Route:
    intent: Intent
    topic: str
    angle: str = "actionable"
    reason: str = ""


_VERB = r"(?:write|draft|create|make|generate|build|produce|turn|convert|put together|give me|design|prepare)"
_ESSAY = re.compile(
    rf"\b(?:ship\s?30|atomic essay|{_VERB}\b[^.?!]{{0,60}}\b(?:essay|article|long[- ]form|newsletter (?:piece|post|issue)|blog post|op-ed|think ?piece))\b",
    re.I,
)
_HTML = re.compile(
    rf"\b(?:{_VERB}\b[^.?!]{{0,80}}\b(?:html|web ?page|landing page|dashboard|one[- ]pager|slide|poster|infographic|styled page|css)|\bas (?:an? )?html\b)",
    re.I,
)
_MD = re.compile(
    rf"\b(?:{_VERB}\b[^.?!]{{0,80}}\b(?:markdown|document|doc\b|checklist|playbook|template|memo|report|brief|table|summary document|artifact|guide|cheat ?sheet|prd|spec)|\bas (?:a )?markdown\b|\bin markdown\b)",
    re.I,
)
_ANGLE = {
    "analytical": re.compile(r"\b(analytical|data[- ]driven|numbers|metrics[- ]heavy)\b", re.I),
    "aspirational": re.compile(r"\b(aspirational|inspir\w+|story of|stories)\b", re.I),
    "anthropological": re.compile(r"\b(anthropolog\w+|why (?:do|people)|psycholog\w+)\b", re.I),
}
_STRIP = re.compile(
    rf"^(?:(?:please|can you|could you|hey|hi)[,\s]*)*(?:{_VERB}\s+(?:me\s+)?(?:an?|the)?\s*)?"
    r"(?:ship\s?30(?: for 30)?(?:[- ]style)?\s*)?(?:essay|article|blog post|markdown|html|web ?page|document|doc|checklist|playbook)?\s*"
    r"(?:about|on|for|explaining|covering|titled)?\s*",
    re.I,
)


def _topic(message: str) -> str:
    t = _STRIP.sub("", message.strip(), count=1).strip(" .?!:\"'")
    return t or message.strip()


def _angle(message: str) -> str:
    for name, rx in _ANGLE.items():
        if rx.search(message):
            return name
    return "actionable"


def route(message: str) -> Route:
    msg = message.strip()
    if _ESSAY.search(msg):
        return Route(Intent.ESSAY, _topic(msg), _angle(msg), "essay keywords")
    if _HTML.search(msg):
        return Route(Intent.ARTIFACT_HTML, _topic(msg), reason="html artifact keywords")
    if _MD.search(msg):
        return Route(Intent.ARTIFACT_MARKDOWN, _topic(msg), reason="markdown artifact keywords")
    return Route(Intent.CHAT, msg, reason="default")
