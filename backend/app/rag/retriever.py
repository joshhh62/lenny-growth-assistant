"""Hybrid retriever: Postgres full-text + pgvector, fused with Reciprocal Rank Fusion.

Guarantees:
  * Works with zero external services beyond Postgres (lexical only).
  * Uses semantic search when the query can be embedded *and* chunks have embeddings.
  * Deduplicates near-adjacent chunks from the same episode so citations are diverse.
  * Returns `Citation` objects with everything the UI needs, including a YouTube
    deep link to the second the passage starts.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass

from app.config import get_settings
from app.db.repository import KnowledgeRepo
from app.logging_setup import get_logger
from app.rag.embeddings import EmbeddingUnavailable, OllamaEmbedder

log = get_logger("retrieval")


@dataclass
class Citation:
    id: int
    episode_id: str
    guest: str
    title: str
    speaker: str | None
    start_seconds: int | None
    end_seconds: int | None
    youtube_url: str | None
    timestamp_url: str | None
    publish_date: str | None
    text: str
    score: float
    sources: list[str]  # which indexes matched: ["lexical"], ["vector"], or both

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def label(self) -> str:
        ts = _fmt_ts(self.start_seconds)
        return f"{self.guest} — {self.title} @ {ts}" if ts else f"{self.guest} — {self.title}"


def _fmt_ts(seconds: int | None) -> str:
    if seconds is None:
        return ""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _row_to_citation(row: dict, score: float, sources: list[str]) -> Citation:
    vid = row.get("video_id")
    start = row.get("start_seconds")
    ts_url = f"https://www.youtube.com/watch?v={vid}&t={int(start)}s" if vid and start is not None else row.get("youtube_url")
    pd = row.get("publish_date")
    return Citation(
        id=int(row["id"]),
        episode_id=row["episode_id"],
        guest=row["guest"],
        title=row["title"],
        speaker=row.get("speaker"),
        start_seconds=start,
        end_seconds=row.get("end_seconds"),
        youtube_url=row.get("youtube_url"),
        timestamp_url=ts_url,
        publish_date=pd.isoformat() if hasattr(pd, "isoformat") else pd,
        text=row["text"],
        score=round(float(score), 4),
        sources=sources,
    )


def rrf_fuse(ranked_lists: dict[str, list[dict]], k: int = 60) -> list[tuple[dict, float, list[str]]]:
    """Reciprocal Rank Fusion across named ranked lists → (row, fused_score, sources)."""
    fused: dict[int, float] = {}
    rows: dict[int, dict] = {}
    srcs: dict[int, list[str]] = {}
    for name, ranked in ranked_lists.items():
        for rank, row in enumerate(ranked):
            cid = int(row["id"])
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank + 1)
            rows.setdefault(cid, row)
            srcs.setdefault(cid, []).append(name)
    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
    return [(rows[cid], score, srcs[cid]) for cid, score in ordered]


_guest_cache: dict[str, list[str]] = {}


class Retriever:
    def __init__(self, repo: KnowledgeRepo, embedder: OllamaEmbedder | None):
        self.repo = repo
        self.embedder = embedder
        self.settings = get_settings()

    async def _mentioned_guests(self, query: str) -> list[str]:
        """Guests named in the query ("What does Elena Verna say…" → ["Elena Verna"]).

        Matches the full name, or a distinctive surname (≥ 5 letters) on its own.
        """
        if "names" not in _guest_cache:
            _guest_cache["names"] = await self.repo.guest_names()
        q = " " + re.sub(r"[^a-z0-9 ]", " ", query.lower()) + " "
        hits: list[str] = []
        for name in _guest_cache["names"]:
            parts = [p for p in re.sub(r"[^a-z0-9 ]", " ", name.lower()).split() if p]
            if not parts:
                continue
            full = " " + " ".join(parts) + " "
            surname = parts[-1]
            if full in q or (len(surname) >= 5 and f" {surname} " in q):
                hits.append(name)
        return hits

    async def search(self, query: str, top_k: int | None = None) -> tuple[list[Citation], dict]:
        """Returns (citations, diagnostics). Never raises for a healthy DB."""
        s = self.settings
        top_k = top_k or s.retrieval_top_k
        pool = top_k * 4
        t0 = time.perf_counter()
        diag: dict = {"query": query, "lexical": 0, "vector": 0, "mode": "lexical"}

        # "What does <guest> say about X" → search that guest's episodes first; fall
        # back to the whole corpus only if they yield too little.
        guests = await self._mentioned_guests(query)
        if guests:
            diag["guests"] = guests
            picked, sub = await self._search_scoped(query, top_k, pool, guests)
            if len(picked) >= 2:
                diag.update(sub)
                diag["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                log.info("retrieval", **{k: v for k, v in diag.items() if k != "query"}, query=query[:120])
                return picked, diag

        picked, sub = await self._search_scoped(query, top_k, pool, None)
        diag.update(sub)
        diag["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        log.info("retrieval", **{k: v for k, v in diag.items() if k != "query"}, query=query[:120])
        return picked, diag

    async def _search_scoped(self, query: str, top_k: int, pool: int, guests: list[str] | None) -> tuple[list[Citation], dict]:
        s = self.settings
        diag: dict = {"lexical": 0, "vector": 0, "mode": "lexical"}
        # NB: one AsyncSession is not safe for concurrent queries, so these run
        # sequentially. Both are single-digit milliseconds on 30k chunks.
        vector_rows: list[dict] = []
        if self.embedder is not None and s.embeddings_enabled:
            try:
                qvec = await asyncio.wait_for(self.embedder.embed_query(query), timeout=8.0)
                raw_rows = await self.repo.vector_search(qvec, pool, guests)
                vector_rows = [r for r in raw_rows if float(r["score"]) >= s.vector_min_similarity]
                diag["vector_filtered"] = len(raw_rows) - len(vector_rows)
                diag["mode"] = "hybrid" if vector_rows else "lexical"
            except (EmbeddingUnavailable, asyncio.TimeoutError) as exc:
                diag["vector_error"] = str(exc)[:200]
        lexical_rows = await self.repo.lexical_search(query, pool, guests)
        diag["lexical"], diag["vector"] = len(lexical_rows), len(vector_rows)

        lists = {"lexical": lexical_rows}
        if vector_rows:
            lists["vector"] = vector_rows
        fused = rrf_fuse(lists)

        # Diversify: at most 2 chunks per episode, and never two overlapping chunks.
        picked: list[Citation] = []
        per_episode: dict[str, list[int]] = {}
        for row, score, sources in fused:
            ep, idx = row["episode_id"], int(row["chunk_index"])
            taken = per_episode.setdefault(ep, [])
            if len(taken) >= 2 or any(abs(idx - t) <= 1 for t in taken):
                continue
            taken.append(idx)
            picked.append(_row_to_citation(row, score, sources))
            if len(picked) >= top_k:
                break
        diag["returned"] = len(picked)
        return picked, diag
