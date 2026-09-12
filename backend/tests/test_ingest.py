"""Unit tests: transcript parsing, chunking, dedupe."""

from pathlib import Path

from app.rag.ingest import Turn, canonical_guest, chunk_turns, dedupe_episodes, parse_transcript

FIX = Path(__file__).parent / "fixtures" / "transcripts" / "episodes"


def test_parse_long_form_timestamps_and_frontmatter():
    ep = parse_transcript(FIX / "elena-verna" / "transcript.md", "episodes/elena-verna/transcript.md")
    assert ep.id == "elena-verna"
    assert ep.video_id == "TESTVID1"
    assert ep.publish_date.isoformat() == "2023-01-15"
    assert ep.keywords == ["retention", "growth"]
    assert [t.speaker for t in ep.turns] == ["Lenny Rachitsky", "Elena Verna", "Lenny Rachitsky", "Elena Verna", "Elena Verna"]
    assert ep.turns[1].start == 12
    # continuation "(00:03:10):" keeps the previous speaker
    assert ep.turns[4].start == 190 and ep.turns[4].speaker == "Elena Verna"


def test_parse_short_form_timestamps():
    ep = parse_transcript(FIX / "casey-winters" / "transcript.md", "x")
    assert [t.start for t in ep.turns] == [12, 31, 79]
    assert ep.turns[2].speaker == "Casey Winters"


def test_parse_inline_bracket_timestamps():
    ep = parse_transcript(FIX / "casey-winters-dup" / "transcript.md", "x")
    assert [(t.speaker, t.start) for t in ep.turns] == [("Lenny", 12), ("Casey", 31)]


def test_chunking_respects_budget_and_keeps_timestamps():
    turns = [Turn("A", i * 10, "word " * 60) for i in range(10)]  # ~79 tokens each
    chunks = chunk_turns(turns, target_tokens=200, overlap_turns=1)
    assert all(c["token_estimate"] <= 200 for c in chunks)
    assert chunks[0]["start_seconds"] == 0
    # overlap: the next chunk starts on the previous chunk's last turn
    assert chunks[1]["start_seconds"] == chunks[0]["end_seconds"]
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_chunking_splits_very_long_single_turn():
    turns = [Turn("A", 0, ("This is a sentence. " * 400).strip())]
    chunks = chunk_turns(turns, target_tokens=150)
    assert len(chunks) > 3
    assert max(c["token_estimate"] for c in chunks) <= 150 * 1.6


def test_canonical_guest_prefers_title_suffix():
    assert canonical_guest("Mastering strategy | Maggie Crowley (Toast, Drift)", "Chip Conley") == "Maggie Crowley"
    assert canonical_guest("No pipe here", "Fallback Name") == "Fallback Name"


def test_dedupe_by_video_id_keeps_matching_slug():
    eps = [parse_transcript(FIX / d / "transcript.md", "x") for d in ("elena-verna", "casey-winters", "casey-winters-dup")]
    kept, dropped = dedupe_episodes(eps)
    assert sorted(e.id for e in kept) == ["casey-winters", "elena-verna"]
    assert dropped == ["casey-winters-dup"]
