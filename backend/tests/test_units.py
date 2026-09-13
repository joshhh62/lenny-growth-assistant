"""Unit tests: router, sanitizer, essay checks, RRF fusion, history trimming."""

import pytest

from app.agent.router import Intent, route
from app.artifacts.sanitize import CSP_META, sanitize_html, sanitize_markdown
from app.rag.retriever import rrf_fuse
from app.skills.ship30.essay import check_essay
from tests.fake_llm import ESSAY


# --- router -----------------------------------------------------------------
@pytest.mark.parametrize(
    "message,intent",
    [
        ("What does Elena Verna say about retention?", Intent.CHAT),
        ("How do I improve activation?", Intent.CHAT),
        ("Write a Ship 30 for 30 essay about product-market fit", Intent.ESSAY),
        ("draft a blog post on pricing", Intent.ESSAY),
        ("Turn that into an analytical essay", Intent.ESSAY),
        ("Make an HTML landing page summarizing the growth loops advice", Intent.ARTIFACT_HTML),
        ("give me this as html", Intent.ARTIFACT_HTML),
        ("Create a markdown checklist for running a good user interview", Intent.ARTIFACT_MARKDOWN),
        ("give me a table comparing the frameworks", Intent.ARTIFACT_MARKDOWN),
        ("Is a document review process worth it?", Intent.CHAT),  # noun without a creation verb
    ],
)
def test_router_intents(message, intent):
    assert route(message).intent == intent


def test_router_topic_and_angle():
    r = route("Write a Ship 30 essay about pricing for B2B SaaS, make it analytical")
    assert r.intent == Intent.ESSAY
    assert r.angle == "analytical"
    assert "pricing" in r.topic.lower()


# --- sanitizer --------------------------------------------------------------
def test_sanitizer_blocks_script_handlers_and_urls():
    html = """<html><head><style>body{color:red;background:url(http://x)}</style>
    <script>alert(1)</script></head><body><h1 onclick="alert(2)">Hi</h1>
    <a href="javascript:alert(3)">j</a><a href="https://youtube.com/w">ok</a>
    <img src="http://evil/t.png"><img src="data:image/png;base64,iVBORw0KGgo=">
    <iframe src="http://evil"></iframe><form><input></form></body></html>"""
    out, rep = sanitize_html(html)
    assert "<script" not in out and "alert(1)" not in out
    assert "onclick" not in out
    assert "javascript:" not in out
    assert 'href="https://youtube.com/w"' in out
    assert "http://evil/t.png" not in out
    assert 'src="data:image/png;base64,iVBORw0KGgo="' in out
    assert "<iframe" not in out and "<form" not in out and "<input" not in out
    assert "url(http://x)" not in out and "color:red" in out
    assert CSP_META in out
    assert set(rep.removed_tags) >= {"script", "iframe", "form", "input", "on*-handlers"}


def test_sanitizer_keeps_document_structure():
    html = "<h1>Title</h1><p class='lead' style='color:#333'>Body <strong>bold</strong></p><table><tr><td colspan='2'>x</td></tr></table>"
    out, rep = sanitize_html(html)
    for frag in ("<h1>Title</h1>", 'class="lead"', "<strong>bold</strong>", 'colspan="2"'):
        assert frag in out
    assert rep.removed_tags == []


def test_sanitizer_lifts_title_into_head():
    """The model emits its own <head><title>; we rebuild the document, so the
    title must end up in <head> — not loose in <body>, which is invalid HTML."""
    html = (
        "<html><head><title>Best Advice on Activation</title>"
        "<style>h1{color:#000}</style></head>"
        "<body><main><h1>Activation</h1></main></body></html>"
    )
    out, _ = sanitize_html(html)
    head, _, body = out.partition("<body>")
    assert "<title>Best Advice on Activation</title>" in head
    assert "<title" not in body
    assert out.index("<title>") < out.index("<style>")  # head order stays sane


def test_sanitizer_without_title_emits_no_empty_title():
    out, _ = sanitize_html("<body><p>hi</p></body>")
    assert "<title" not in out


def test_sanitizer_truncates_oversized():
    out, rep = sanitize_html("<p>" + "a" * 5000 + "</p>", max_bytes=1000)
    assert rep.truncated and len(out) < 2000


def test_markdown_sanitizer_strips_script():
    md, rep = sanitize_markdown("# T\n<script>x</script>\n- ok")
    assert "<script" not in md and "- ok" in md and rep.removed_tags == ["script"]


# --- essay checks -----------------------------------------------------------
def test_essay_check_passes_canned_essay():
    chk = check_essay(ESSAY, n_passages=6)
    assert chk.ok, chk.problems
    assert 1000 <= chk.word_count <= 1500


def test_essay_check_flags_bad_essay():
    chk = check_essay("# T\n\nShort essay with no structure.", n_passages=3)
    assert not chk.ok
    assert any("word count" in p for p in chk.problems)
    assert any("citations" in p for p in chk.problems)


def test_essay_check_flags_unknown_citations():
    chk = check_essay(ESSAY.replace("[1]", "[9]"), n_passages=6)
    assert any("unknown passages" in p for p in chk.problems)


# --- retrieval fusion -------------------------------------------------------
def test_rrf_fusion_prefers_items_in_both_lists():
    lex = [{"id": 1}, {"id": 2}, {"id": 3}]
    vec = [{"id": 3}, {"id": 4}, {"id": 1}]
    fused = rrf_fuse({"lexical": lex, "vector": vec})
    order = [r["id"] for r, _, _ in fused]
    assert order[:2] == [1, 3] or order[:2] == [3, 1]
    srcs = {r["id"]: s for r, _, s in fused}
    assert srcs[1] == ["lexical", "vector"] and srcs[4] == ["vector"]


# --- history trimming -------------------------------------------------------
def test_history_trimming_respects_budget_and_starts_with_user():
    from app.agent.service import ChatService

    hist = [{"role": "assistant", "content": "orphan"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "w " * 300} for i in range(10)
    ]
    out = ChatService._history_messages(ChatService.__new__(ChatService), hist, budget_tokens=1000)
    assert out[0]["role"] == "user"
    assert len(out) <= 3


# --- essay truncation -------------------------------------------------------
def test_essay_check_detects_truncated_output():
    """A generation that hits its token ceiling stops mid-sentence. The checker
    must catch it so the pipeline retries instead of shipping half an essay."""
    truncated = ESSAY.rsplit(".", 2)[0] + ".\n\nFinding product-market fit once is not the achievement — defending it, on a sh"
    chk = check_essay(truncated, n_passages=6)
    assert chk.truncated
    assert any("truncated" in p for p in chk.problems)
    assert not chk.ok


def test_essay_check_accepts_complete_output():
    assert not check_essay(ESSAY, n_passages=6).truncated


def test_sources_block_lists_only_cited_passages():
    from app.rag.retriever import Citation
    from app.skills.ship30.essay import sources_block

    def cite(n: int) -> Citation:
        return Citation(id=n, episode_id=f"ep{n}", guest=f"Guest {n}", title=f"Title {n}",
                        speaker=None, start_seconds=60, end_seconds=90, youtube_url=None,
                        timestamp_url=f"https://youtu.be/x?t={n}", publish_date=None,
                        text="…", score=1.0, sources=["lexical"])

    cites = [cite(1), cite(2), cite(3)]
    block = sources_block(cites, used={1, 3})
    assert "Guest 1" in block and "Guest 3" in block
    assert "Guest 2" not in block          # never referenced in the body
    assert "[3]" in block                  # numbering stays stable
