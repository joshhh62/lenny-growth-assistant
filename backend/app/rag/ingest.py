"""Transcript ingestion.

Pipeline (see architecture.md → Ingestion):
  1. Locate transcripts (`TRANSCRIPTS_DIR`); clone the public repo if absent.
  2. Parse each `episodes/<slug>/transcript.md`: YAML frontmatter + timestamped turns.
  3. Chunk by speaker turn, packing turns until ~`chunk_target_tokens`, with a
     one-turn overlap so an answer that straddles a boundary is still recoverable.
     Every chunk keeps speaker + start/end seconds → citations deep-link to YouTube.
  4. Upsert into Postgres. A sha256 of the file detects changed/new episodes, so
     re-running is an incremental refresh, not a rebuild.
  5. Embeddings are *not* computed here; a background worker fills them in
     (see embeddings.py) so lexical search works the moment ingest finishes.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger("ingest")

# Bump when parsing/chunking changes so `content_hash` no longer matches and
# every episode is re-ingested on the next run (refresh detection is hash-based).
PARSER_VERSION = "5"
AD_RE = re.compile(
    r"brought to you by|this episode is sponsored|today's episode is sponsored|use (?:the )?code \w+|"
    r"lennysnewsletter\.com|subscribe (?:to|and follow)|check (?:it|them) out at [\w./-]+|"
    r"visit [\w.-]+\.com|go to [\w.-]+\.com/\w+",
    re.I,
)

# Matches "Speaker Name (00:12:34):", "(00:12:34):" and short-form "(12:34):".
TURN_RE = re.compile(
    r"^(?:(?P<speaker>[^\n(]{1,80}?)\s)?\((?:(?P<h>\d{1,2}):)?(?P<m>\d{1,2}):(?P<s>\d{2})\):\s*$"
)
NO_TS_TURN_RE = re.compile(r"^(?P<speaker>[A-Z][A-Za-z.'\- ]{1,60}):\s*$")
INLINE_TS_RE = re.compile(
    r"^\[(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\]\s*(?:(?P<speaker>[A-Z][A-Za-z.'\- ]{1,40}):\s*)?(?P<text>.+)$"
)


@dataclass
class Turn:
    speaker: str
    start: int
    text: str


@dataclass
class Episode:
    id: str
    guest: str
    title: str
    youtube_url: str | None
    video_id: str | None
    publish_date: date | None
    duration_seconds: int | None
    keywords: list[str]
    source_path: str
    content_hash: str
    turns: list[Turn] = field(default_factory=list)


def estimate_tokens(text: str) -> int:
    # Cheap, model-agnostic estimate (~1.3 tokens per word for English prose).
    return int(len(text.split()) * 1.3) + 1


def ensure_transcripts(root: Path, repo_url: str) -> Path:
    if (root / "episodes").is_dir():
        return root
    root.parent.mkdir(parents=True, exist_ok=True)
    log.info("cloning_transcripts", url=repo_url, dest=str(root))
    subprocess.run(["git", "clone", "--depth", "1", repo_url, str(root)], check=True, timeout=600)
    return root


def parse_transcript(path: Path, rel_path: str) -> Episode:
    raw = path.read_text(encoding="utf-8", errors="replace")
    content_hash = hashlib.sha256((PARSER_VERSION + "\n" + raw).encode("utf-8")).hexdigest()
    meta: dict = {}
    body = raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            try:
                meta = yaml.safe_load(raw[3:end]) or {}
            except yaml.YAMLError:  # a handful of files have odd quoting
                meta = {}
            body = raw[end + 4:]

    turns: list[Turn] = []
    speaker = "Unknown"
    cur_start: int | None = None
    buf: list[str] = []

    def flush() -> None:
        if cur_start is not None and buf:
            txt = " ".join(x.strip() for x in buf if x.strip())
            if txt:
                turns.append(Turn(speaker=speaker, start=cur_start, text=txt))

    for line in body.splitlines():
        stripped = line.strip()
        m = TURN_RE.match(stripped)
        m2 = None if m else NO_TS_TURN_RE.match(stripped)
        m3 = None if (m or m2) else INLINE_TS_RE.match(stripped)
        if m3:  # "[00:12:34] Speaker: text" — whole turn on one line
            flush()
            buf = [m3.group("text")]
            if m3.group("speaker"):
                speaker = m3.group("speaker").strip()
            cur_start = int(m3.group("h")) * 3600 + int(m3.group("m")) * 60 + int(m3.group("s"))
            continue
        if m:
            flush()
            buf = []
            if m.group("speaker"):
                speaker = m.group("speaker").strip()
            cur_start = int(m.group("h") or 0) * 3600 + int(m.group("m")) * 60 + int(m.group("s"))
        elif m2:  # a few transcripts have "Speaker:" lines with no timestamps
            flush()
            buf = []
            speaker = m2.group("speaker").strip()
            cur_start = 0
        elif line.startswith("#"):
            continue
        else:
            buf.append(line)
    flush()

    pd = meta.get("publish_date")
    if isinstance(pd, str):
        try:
            pd = date.fromisoformat(pd[:10])
        except ValueError:
            pd = None
    dur = meta.get("duration_seconds")
    return Episode(
        id=path.parent.name,
        guest=str(meta.get("guest") or path.parent.name.replace("-", " ").title()),
        title=str(meta.get("title") or path.parent.name),
        youtube_url=meta.get("youtube_url"),
        video_id=meta.get("video_id"),
        publish_date=pd if isinstance(pd, date) else None,
        duration_seconds=int(float(dur)) if dur else None,
        keywords=[str(k) for k in (meta.get("keywords") or [])],
        source_path=rel_path,
        content_hash=content_hash,
        turns=turns,
    )


def chunk_turns(turns: list[Turn], target_tokens: int, overlap_turns: int = 1) -> list[dict]:
    """Pack consecutive turns into chunks of ~target_tokens, overlapping by N turns.

    Very long single turns are split on sentence boundaries so no chunk greatly
    exceeds the target (keeps embedding inputs and prompt budgets predictable).
    """
    units: list[Turn] = []
    for t in turns:
        if estimate_tokens(t.text) <= target_tokens * 1.5:
            units.append(t)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", t.text)
        piece: list[str] = []
        for s in sentences:
            piece.append(s)
            if estimate_tokens(" ".join(piece)) >= target_tokens:
                units.append(Turn(t.speaker, t.start, " ".join(piece)))
                piece = []
        if piece:
            units.append(Turn(t.speaker, t.start, " ".join(piece)))

    chunks: list[dict] = []
    i = 0
    while i < len(units):
        j = i
        toks = 0
        while j < len(units) and (toks == 0 or toks + estimate_tokens(units[j].text) <= target_tokens):
            toks += estimate_tokens(units[j].text)
            j += 1
        group = units[i:j]
        speakers = list(dict.fromkeys(u.speaker for u in group))
        text = "\n\n".join(f"{u.speaker}: {u.text}" for u in group)
        chunks.append(
            {
                "chunk_index": len(chunks),
                "speaker": ", ".join(speakers)[:200],
                "start_seconds": group[0].start,
                "end_seconds": group[-1].start,
                "text": text,
                "token_estimate": toks,
                "is_ad": bool(AD_RE.search(text)),
            }
        )
        if j >= len(units):
            break
        i = max(j - overlap_turns, i + 1)
    return chunks


_TITLE_GUEST = re.compile(r"\|\s*([^|(]+?)\s*(?:\(.*\))?\s*$")
_NAME_LIKE = re.compile(r"^(?:[A-Z][\w.'’\-]*|and|&|\+|de|van|von|da|of)(?:\s+(?:[A-Z][\w.'’\-]*|and|&|\+|de|van|von|da|of)){0,6}$")


def canonical_guest(title: str, fallback: str) -> str:
    """The repo's `guest` frontmatter is unreliable for ~30 mis-filed folders;
    the title suffix ("… | Guest Name (Company)") is correct whenever it looks
    like a person's name. Titles with a non-name suffix ("… | How Lovable hit
    $200M ARR") keep the frontmatter guest."""
    m = _TITLE_GUEST.search(title or "")
    if m:
        cand = m.group(1).strip()
        if 2 <= len(cand) <= 60 and _NAME_LIKE.match(cand) and not any(ch.isdigit() for ch in cand):
            return cand
    # Frontmatter uses "Elena Verna 4.0" for repeat guests; the number is not part of the name.
    return re.sub(r"\s+\d+(?:\.\d+)?$", "", fallback).strip() or fallback


def dedupe_episodes(episodes: list[Episode]) -> tuple[list[Episode], list[str]]:
    """Collapse folders that point at the same YouTube video (source-repo quirk).

    Winner: the folder whose slug matches the title-derived guest; tie-break on
    the longer transcript. Returns (kept, dropped_ids).
    """
    by_video: dict[str, list[Episode]] = {}
    kept: list[Episode] = []
    for ep in episodes:
        if ep.video_id:
            by_video.setdefault(ep.video_id, []).append(ep)
        else:
            kept.append(ep)
    dropped: list[str] = []
    for vid, group in by_video.items():
        if len(group) == 1:
            kept.append(group[0])
            continue

        def score(e: Episode) -> tuple[int, int]:
            slug_guess = re.sub(r"[^a-z]", "", canonical_guest(e.title, e.guest).lower())
            slug = re.sub(r"[^a-z]", "", e.id.lower())
            match = 1 if slug_guess and (slug_guess in slug or slug in slug_guess) else 0
            return (match, sum(len(t.text) for t in e.turns))

        group.sort(key=score, reverse=True)
        kept.append(group[0])
        dropped.extend(e.id for e in group[1:])
    return kept, dropped


def iter_episode_files(root: Path, limit: int = 0):
    files = sorted((root / "episodes").glob("*/transcript.md"))
    if limit:
        files = files[:limit]
    for f in files:
        yield f, str(f.relative_to(root))


async def run_ingest(db_sessionmaker, *, limit: int | None = None) -> dict:
    """Incremental ingest. Returns summary counts."""
    from app.db.repository import KnowledgeRepo

    s = get_settings()
    root = ensure_transcripts(Path(s.transcripts_dir), s.transcripts_repo_url)
    limit = s.ingest_episode_limit if limit is None else limit

    async with db_sessionmaker() as db:
        repo = KnowledgeRepo(db)
        run_id = await repo.start_ingest_run()
        await db.commit()
        known = await repo.episode_hashes()
        seen = new = chunks_written = 0
        try:
            parsed = [parse_transcript(path, rel) for path, rel in iter_episode_files(root, limit)]
            seen = len(parsed)
            episodes, dropped = dedupe_episodes(parsed)
            if dropped:
                log.info("ingest_deduped", dropped=len(dropped), examples=dropped[:5])
                await repo.delete_episodes(dropped)
            for ep in episodes:
                ep.guest = canonical_guest(ep.title, ep.guest)
                if known.get(ep.id) == ep.content_hash:
                    continue
                chunks = chunk_turns(ep.turns, s.chunk_target_tokens, s.chunk_overlap_turns)
                await repo.upsert_episode(
                    {
                        "id": ep.id, "guest": ep.guest, "title": ep.title,
                        "youtube_url": ep.youtube_url, "video_id": ep.video_id,
                        "publish_date": ep.publish_date, "duration_seconds": ep.duration_seconds,
                        "keywords": ep.keywords, "source_path": ep.source_path,
                        "content_hash": ep.content_hash,
                    },
                    chunks,
                )
                await db.commit()
                new += 1
                chunks_written += len(chunks)
                if new % 25 == 0:
                    log.info("ingest_progress", episodes_new=new, chunks=chunks_written)
            await repo.finish_ingest_run(
                run_id, status="ok", episodes_seen=seen, episodes_new=new, chunks_written=chunks_written
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            await repo.finish_ingest_run(run_id, status="failed", error=str(exc)[:500],
                                         episodes_seen=seen, episodes_new=new, chunks_written=chunks_written)
            await db.commit()
            log.exception("ingest_failed")
            raise
    summary = {"episodes_seen": seen, "episodes_new": new, "chunks_written": chunks_written}
    log.info("ingest_complete", **summary)
    return summary
