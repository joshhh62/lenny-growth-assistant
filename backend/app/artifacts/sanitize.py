"""Artifact sanitization — defence in depth for model-generated HTML.

Threat model: the LLM (or a prompt-injected transcript passage) emits HTML that
tries to run script, phone home, or break out of the viewer. We treat every
artifact as untrusted and apply TWO independent layers:

  Layer 1 (this module, server-side, on write):
    * Parse with a real HTML parser (bleach/html5lib) — no regex-only stripping.
    * Allow-list of tags and attributes appropriate for documents & styled pages.
    * Drop <script>, <iframe>, <object>, <embed>, <form>, <link>, <meta>,
      event handlers (on*), javascript:/data: URLs, and external resource loads.
    * Keep a single inline <style> block but strip @import, url(), expression().

  Layer 2 (frontend, on render):
    * Render inside <iframe sandbox=""> — the empty sandbox gives the document an
      opaque origin with no scripts, no forms, no top-navigation, no popups and
      no access to the parent's cookies or storage.
    * A strict Content-Security-Policy <meta> is injected into every document:
      default-src 'none'; style-src 'unsafe-inline'; img-src data:  — so even if
      markup slipped through Layer 1, the browser refuses to load or execute it.

Either layer alone would be a reasonable control; together, a bypass of one is
contained by the other. Markdown artifacts are rendered with a Markdown library
in "no raw HTML" mode and then pass through the same allow-list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import bleach
from bleach.css_sanitizer import CSSSanitizer

ALLOWED_TAGS = {
    # document structure
    "html", "head", "body", "title", "style", "main", "header", "footer", "nav", "section",
    "article", "aside", "div", "span", "p", "br", "hr", "blockquote", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "em", "i", "u", "s", "small", "sup", "sub",
    "mark", "abbr", "cite", "q", "kbd", "figure", "figcaption",
    # lists & tables
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    # media (inline only — img src limited to data: below)
    "img", "svg", "path", "circle", "rect", "line", "polyline", "polygon", "g", "text", "defs",
    "linearGradient", "stop",
    # links
    "a", "details", "summary",
}

GLOBAL_ATTRS = {"class", "id", "style", "title", "lang", "dir", "role", "aria-label", "aria-hidden"}
ALLOWED_ATTRS = {
    "*": GLOBAL_ATTRS,
    "a": GLOBAL_ATTRS | {"href", "target", "rel"},
    "img": GLOBAL_ATTRS | {"src", "alt", "width", "height"},
    "td": GLOBAL_ATTRS | {"colspan", "rowspan"},
    "th": GLOBAL_ATTRS | {"colspan", "rowspan", "scope"},
    "col": GLOBAL_ATTRS | {"span"},
    "details": GLOBAL_ATTRS | {"open"},
    "svg": GLOBAL_ATTRS | {"viewBox", "width", "height", "xmlns", "fill", "stroke"},
    "path": GLOBAL_ATTRS | {"d", "fill", "stroke", "stroke-width"},
    "circle": GLOBAL_ATTRS | {"cx", "cy", "r", "fill", "stroke"},
    "rect": GLOBAL_ATTRS | {"x", "y", "width", "height", "rx", "fill", "stroke"},
    "line": GLOBAL_ATTRS | {"x1", "y1", "x2", "y2", "stroke", "stroke-width"},
    "polyline": GLOBAL_ATTRS | {"points", "fill", "stroke"},
    "polygon": GLOBAL_ATTRS | {"points", "fill", "stroke"},
    "text": GLOBAL_ATTRS | {"x", "y", "fill", "font-size", "text-anchor"},
    "g": GLOBAL_ATTRS | {"fill", "stroke", "transform"},
    "linearGradient": GLOBAL_ATTRS | {"x1", "y1", "x2", "y2"},
    "stop": GLOBAL_ATTRS | {"offset", "stop-color"},
}
ALLOWED_PROTOCOLS = ["https", "http", "mailto", "data"]  # data: re-validated below (images only)
_DROP_WITH_CONTENT = re.compile(
    r"<(script|iframe|object|embed|noscript|template|form)\b[^>]*>.*?</\1\s*>", re.I | re.S
)

_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style>", re.I | re.S)
_CSS_DANGEROUS = re.compile(r"@import|expression\s*\(|url\s*\(|behavior\s*:|-moz-binding|javascript:", re.I)
_DATA_IMG = re.compile(r"^data:image/(png|jpeg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$")

CSP_META = (
    '<meta http-equiv="Content-Security-Policy" '
    "content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; "
    "script-src 'none'; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'\">"
)


@dataclass
class SanitizeReport:
    removed_tags: list[str] = field(default_factory=list)
    removed_css_rules: int = 0
    stripped_urls: int = 0
    truncated: bool = False

    def to_dict(self) -> dict:
        return self.__dict__


def _clean_css(css: str, report: SanitizeReport) -> str:
    # Remove whole declarations containing dangerous constructs, keep the rest.
    out_rules = []
    for rule in css.split("}"):
        if _CSS_DANGEROUS.search(rule):
            # drop only offending declarations
            decls = rule.split(";")
            kept = [d for d in decls if not _CSS_DANGEROUS.search(d)]
            report.removed_css_rules += len(decls) - len(kept)
            rule = ";".join(kept)
        if rule.strip():
            out_rules.append(rule)
    return "}".join(out_rules) + ("}" if out_rules else "")


def sanitize_html(html: str, max_bytes: int = 200_000) -> tuple[str, SanitizeReport]:
    report = SanitizeReport()
    if len(html.encode("utf-8")) > max_bytes:
        html = html.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        report.truncated = True

    # Detect what we're about to remove, for observability.
    for tag in ("script", "iframe", "object", "embed", "form", "link", "meta", "base", "input", "button"):
        if re.search(rf"<{tag}\b", html, re.I):
            report.removed_tags.append(tag)
    if re.search(r"\son\w+\s*=", html, re.I):
        report.removed_tags.append("on*-handlers")

    # Remove dangerous elements *with their contents* (bleach's strip=True would
    # otherwise keep "alert(1)" as visible text).
    html = _DROP_WITH_CONTENT.sub("", html)

    # Pull out <style> blocks, clean their CSS, and re-insert after bleach.
    styles = [_clean_css(m.group(1), report) for m in _STYLE_BLOCK.finditer(html)]
    html_wo_style = _STYLE_BLOCK.sub("", html)

    css_sanitizer = CSSSanitizer()  # allow-lists inline style properties
    cleaner = bleach.Cleaner(
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
        css_sanitizer=css_sanitizer,
        filters=[],
    )
    cleaned = cleaner.clean(html_wo_style)

    # Images: only inline base64 raster/svg images survive (the CSP forbids
    # network loads anyway, so a remote src would just be a broken image + a
    # tracking attempt). Links: never data:/javascript: (bleach drops javascript:).
    def _fix_img(m: re.Match) -> str:
        src = m.group(1)
        if _DATA_IMG.match(src):
            return m.group(0)
        report.stripped_urls += 1
        return m.group(0).replace(src, "")

    cleaned = re.sub(r'<img[^>]*\ssrc="([^"]*)"', _fix_img, cleaned)

    def _fix_href(m: re.Match) -> str:
        href = m.group(1)
        if href.startswith(("https://", "http://", "mailto:")):
            return m.group(0)
        report.stripped_urls += 1
        return m.group(0).replace(href, "")

    cleaned = re.sub(r'<a[^>]*\shref="([^"]*)"', _fix_href, cleaned)

    # Assemble a full document with the CSP meta and cleaned styles.
    style_tag = f"<style>{' '.join(styles)}</style>" if styles else ""
    body = cleaned
    body = re.sub(r"</?(html|head|body)\b[^>]*>", "", body, flags=re.I)

    # The model writes <title> inside its own <head>; we discard that wrapper and
    # rebuild the document, so an unhandled <title> would land in <body> (invalid
    # HTML, and some browsers render it as text). Lift it back into <head>.
    title_tag = ""
    m_title = re.search(r"<title\b[^>]*>(.*?)</title\s*>", body, flags=re.I | re.S)
    if m_title:
        title_text = re.sub(r"\s+", " ", m_title.group(1)).strip()
        if title_text:
            title_tag = f"<title>{title_text}</title>"
        body = re.sub(r"<title\b[^>]*>.*?</title\s*>", "", body, flags=re.I | re.S)
    body = re.sub(r"<title\b[^>]*/?>", "", body, flags=re.I)  # stray/unclosed

    # Collapse the blank lines left behind by the stripped head elements.
    body = re.sub(r"\n{3,}", "\n\n", body).strip("\n")

    doc = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        + CSP_META
        + "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        + title_tag
        + style_tag
        + "</head><body>"
        + body
        + "</body></html>"
    )
    return doc, report


def sanitize_markdown(md: str, max_bytes: int = 200_000) -> tuple[str, SanitizeReport]:
    """Markdown is rendered client-side with raw HTML disabled; here we only
    bound size and strip HTML tags that some models sneak into Markdown."""
    report = SanitizeReport()
    if len(md.encode("utf-8")) > max_bytes:
        md = md.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        report.truncated = True
    if re.search(r"<script\b", md, re.I):
        report.removed_tags.append("script")
    md = re.sub(r"<script\b.*?</script>", "", md, flags=re.I | re.S)
    md = re.sub(r"<(iframe|object|embed|form)\b[^>]*>.*?</\1>", "", md, flags=re.I | re.S)
    return md, report


def sanitize(kind: str, content: str, max_bytes: int) -> tuple[str, SanitizeReport]:
    if kind == "html":
        return sanitize_html(content, max_bytes)
    return sanitize_markdown(content, max_bytes)
