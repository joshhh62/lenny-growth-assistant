"""Persistence layer. Plain SQL via SQLAlchemy Core so the queries are auditable.

Two repositories: `ConversationRepo` (users/sessions/messages/artifacts) and
`KnowledgeRepo` (episodes/chunks + hybrid search).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class ConversationRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    # --- users -------------------------------------------------------------
    async def upsert_user(self, user_id: str, display_name: str | None, metadata: dict) -> None:
        await self.db.execute(
            text(
                """
                INSERT INTO users (id, display_name, metadata)
                VALUES (:id, :name, CAST(:meta AS jsonb))
                ON CONFLICT (id) DO UPDATE SET
                    display_name = COALESCE(EXCLUDED.display_name, users.display_name),
                    metadata = users.metadata || EXCLUDED.metadata,
                    last_seen_at = now()
                """
            ),
            {"id": user_id, "name": display_name, "meta": json.dumps(metadata)},
        )

    # --- sessions ----------------------------------------------------------
    async def create_session(
        self, *, user_id: str | None, provider: str, model: str, title: str, metadata: dict
    ) -> dict:
        sid = _uuid()
        row = (
            await self.db.execute(
                text(
                    """
                    INSERT INTO sessions (id, user_id, title, provider, model, metadata)
                    VALUES (:id, :uid, :title, :provider, :model, CAST(:meta AS jsonb))
                    RETURNING id, user_id, title, provider, model, metadata, created_at, updated_at
                    """
                ),
                {
                    "id": sid, "uid": user_id, "title": title, "provider": provider,
                    "model": model, "meta": json.dumps(metadata),
                },
            )
        ).mappings().one()
        return dict(row)

    async def get_session(self, session_id: uuid.UUID) -> dict | None:
        row = (
            await self.db.execute(
                text("SELECT * FROM sessions WHERE id = :id"), {"id": session_id}
            )
        ).mappings().one_or_none()
        return dict(row) if row else None

    async def list_sessions(self, user_id: str | None, limit: int = 50) -> list[dict]:
        q = """
            SELECT s.*, (SELECT count(*) FROM messages m WHERE m.session_id = s.id) AS message_count
            FROM sessions s
            {where}
            ORDER BY s.updated_at DESC LIMIT :limit
        """
        where = "WHERE s.user_id = :uid" if user_id else ""
        rows = (
            await self.db.execute(text(q.format(where=where)), {"uid": user_id, "limit": limit})
        ).mappings().all()
        return [dict(r) for r in rows]

    async def update_session(self, session_id: uuid.UUID, **fields: Any) -> dict | None:
        allowed = {k: v for k, v in fields.items() if k in {"title", "provider", "model"} and v is not None}
        if not allowed:
            return await self.get_session(session_id)
        sets = ", ".join(f"{k} = :{k}" for k in allowed)
        row = (
            await self.db.execute(
                text(f"UPDATE sessions SET {sets}, updated_at = now() WHERE id = :id RETURNING *"),
                {"id": session_id, **allowed},
            )
        ).mappings().one_or_none()
        return dict(row) if row else None

    async def touch_session(self, session_id: uuid.UUID) -> None:
        await self.db.execute(
            text("UPDATE sessions SET updated_at = now() WHERE id = :id"), {"id": session_id}
        )

    async def delete_session(self, session_id: uuid.UUID) -> bool:
        res = await self.db.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_id})
        return res.rowcount > 0  # type: ignore[attr-defined]

    # --- messages ----------------------------------------------------------
    async def add_message(
        self,
        session_id: uuid.UUID,
        role: str,
        content: str,
        *,
        citations: Sequence[dict] = (),
        tool_calls: Sequence[dict] = (),
        provider: str | None = None,
        model: str | None = None,
        runtime: str | None = None,
        latency_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        error: str | None = None,
    ) -> dict:
        mid = _uuid()
        row = (
            await self.db.execute(
                text(
                    """
                    INSERT INTO messages (id, session_id, role, content, citations, tool_calls,
                        provider, model, runtime, latency_ms, input_tokens, output_tokens, error)
                    VALUES (:id, :sid, :role, :content, CAST(:cit AS jsonb), CAST(:tc AS jsonb),
                        :provider, :model, :runtime, :latency, :itok, :otok, :error)
                    RETURNING *
                    """
                ),
                {
                    "id": mid, "sid": session_id, "role": role, "content": content,
                    "cit": json.dumps(list(citations)), "tc": json.dumps(list(tool_calls)),
                    "provider": provider, "model": model, "runtime": runtime,
                    "latency": latency_ms, "itok": input_tokens, "otok": output_tokens,
                    "error": error,
                },
            )
        ).mappings().one()
        await self.touch_session(session_id)
        return dict(row)

    async def list_messages(self, session_id: uuid.UUID, limit: int = 200) -> list[dict]:
        rows = (
            await self.db.execute(
                text(
                    "SELECT * FROM messages WHERE session_id = :sid ORDER BY created_at ASC LIMIT :limit"
                ),
                {"sid": session_id, "limit": limit},
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    # --- artifacts ---------------------------------------------------------
    async def add_artifact(
        self, session_id: uuid.UUID, message_id: uuid.UUID | None, kind: str, title: str, content: str
    ) -> dict:
        row = (
            await self.db.execute(
                text(
                    """
                    INSERT INTO artifacts (id, session_id, message_id, kind, title, content)
                    VALUES (:id, :sid, :mid, :kind, :title, :content) RETURNING *
                    """
                ),
                {"id": _uuid(), "sid": session_id, "mid": message_id, "kind": kind,
                 "title": title, "content": content},
            )
        ).mappings().one()
        return dict(row)

    async def get_artifact(self, artifact_id: uuid.UUID) -> dict | None:
        row = (
            await self.db.execute(text("SELECT * FROM artifacts WHERE id = :id"), {"id": artifact_id})
        ).mappings().one_or_none()
        return dict(row) if row else None

    async def list_artifacts(self, session_id: uuid.UUID) -> list[dict]:
        rows = (
            await self.db.execute(
                text("SELECT id, session_id, message_id, kind, title, created_at FROM artifacts "
                     "WHERE session_id = :sid ORDER BY created_at DESC"),
                {"sid": session_id},
            )
        ).mappings().all()
        return [dict(r) for r in rows]


class KnowledgeRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def episode_hashes(self) -> dict[str, str]:
        rows = (await self.db.execute(text("SELECT id, content_hash FROM episodes"))).all()
        return {r[0]: r[1] for r in rows}

    async def upsert_episode(self, ep: dict, chunks: list[dict]) -> None:
        await self.db.execute(text("DELETE FROM chunks WHERE episode_id = :id"), {"id": ep["id"]})
        await self.db.execute(
            text(
                """
                INSERT INTO episodes (id, guest, title, youtube_url, video_id, publish_date,
                    duration_seconds, keywords, source_path, content_hash, chunk_count, ingested_at)
                VALUES (:id, :guest, :title, :youtube_url, :video_id, :publish_date,
                    :duration_seconds, CAST(:keywords AS jsonb), :source_path, :content_hash, :n, now())
                ON CONFLICT (id) DO UPDATE SET
                    guest = EXCLUDED.guest, title = EXCLUDED.title, youtube_url = EXCLUDED.youtube_url,
                    video_id = EXCLUDED.video_id, publish_date = EXCLUDED.publish_date,
                    duration_seconds = EXCLUDED.duration_seconds, keywords = EXCLUDED.keywords,
                    source_path = EXCLUDED.source_path, content_hash = EXCLUDED.content_hash,
                    chunk_count = EXCLUDED.chunk_count, ingested_at = now()
                """
            ),
            {**ep, "keywords": json.dumps(ep.get("keywords", [])), "n": len(chunks)},
        )
        if chunks:
            await self.db.execute(
                text(
                    """
                    INSERT INTO chunks (episode_id, chunk_index, speaker, start_seconds, end_seconds,
                        text, token_estimate, is_ad)
                    VALUES (:episode_id, :chunk_index, :speaker, :start_seconds, :end_seconds,
                        :text, :token_estimate, :is_ad)
                    """
                ),
                [{"episode_id": ep["id"], **c} for c in chunks],
            )

    async def delete_episodes(self, ids: list[str]) -> None:
        if ids:
            await self.db.execute(text("DELETE FROM episodes WHERE id = ANY(:ids)"), {"ids": ids})

    async def start_ingest_run(self) -> int:
        row = (await self.db.execute(text("INSERT INTO ingest_runs DEFAULT VALUES RETURNING id"))).one()
        return int(row[0])

    async def finish_ingest_run(self, run_id: int, **fields: Any) -> None:
        await self.db.execute(
            text(
                "UPDATE ingest_runs SET finished_at = now(), status = :status, error = :error,"
                " episodes_seen = :seen, episodes_new = :new, chunks_written = :chunks WHERE id = :id"
            ),
            {"id": run_id, "status": fields.get("status", "ok"), "error": fields.get("error"),
             "seen": fields.get("episodes_seen", 0), "new": fields.get("episodes_new", 0),
             "chunks": fields.get("chunks_written", 0)},
        )

    async def last_ingest_run(self) -> dict | None:
        row = (
            await self.db.execute(text("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1"))
        ).mappings().one_or_none()
        return dict(row) if row else None

    async def stats(self) -> dict:
        row = (
            await self.db.execute(
                text(
                    "SELECT (SELECT count(*) FROM episodes) AS episodes,"
                    " (SELECT count(*) FROM chunks) AS chunks,"
                    " (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) AS embedded,"
                    " (SELECT max(ingested_at) FROM episodes) AS last_ingested_at"
                )
            )
        ).mappings().one()
        return dict(row)

    # --- embeddings worker -------------------------------------------------
    async def unembedded_chunks(self, limit: int) -> list[dict]:
        rows = (
            await self.db.execute(
                text("SELECT id, text FROM chunks WHERE embedding IS NULL ORDER BY id LIMIT :n"),
                {"n": limit},
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    async def write_embeddings(self, pairs: list[tuple[int, list[float]]]) -> None:
        await self.db.execute(
            text("UPDATE chunks SET embedding = CAST(:emb AS vector) WHERE id = :id"),
            [{"id": cid, "emb": "[" + ",".join(f"{x:.6f}" for x in vec) + "]"} for cid, vec in pairs],
        )

    # --- search ------------------------------------------------------------
    # Chunk relevance (cover density) plus a boost when the episode *title*
    # matches the query — an episode about the topic should outrank a passing mention.
    _LEXICAL_SQL = """
        SELECT c.id, c.episode_id, c.chunk_index, c.speaker, c.start_seconds, c.end_seconds,
               c.text, e.guest, e.title, e.youtube_url, e.video_id, e.publish_date,
               ts_rank_cd(c.tsv, q, 32) + 0.5 * ts_rank(e.title_tsv, q) AS score
        FROM chunks c
        JOIN episodes e ON e.id = c.episode_id,
             {tsquery} q
        WHERE c.tsv @@ q AND NOT c.is_ad {guest_filter}
        ORDER BY score DESC
        LIMIT :limit
    """

    async def guest_names(self) -> list[str]:
        rows = (await self.db.execute(text("SELECT DISTINCT guest FROM episodes"))).all()
        return [r[0] for r in rows if r[0]]

    async def lexical_search(self, query: str, limit: int, guests: list[str] | None = None) -> list[dict]:
        """Postgres full-text search (ts_rank_cd, cover density).

        Strict AND semantics first (websearch_to_tsquery); if that under-fills,
        relax to OR over the same terms so partial matches still surface, ranked
        below the strict hits.
        """
        gf = "AND e.guest = ANY(:guests)" if guests else ""
        params: dict = {"q": query, "limit": limit}
        if guests:
            params["guests"] = guests
        strict = (
            await self.db.execute(
                text(self._LEXICAL_SQL.format(tsquery="websearch_to_tsquery('english', :q)", guest_filter=gf)),
                params,
            )
        ).mappings().all()
        rows = [dict(r) for r in strict]
        if len(rows) >= limit:
            return rows
        terms = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]+", query) if len(t) > 2]
        if not terms:
            return rows
        or_query = " OR ".join(terms)
        relaxed = (
            await self.db.execute(
                text(self._LEXICAL_SQL.format(tsquery="websearch_to_tsquery('english', :q)", guest_filter=gf)),
                {**params, "q": or_query, "limit": limit * 2},
            )
        ).mappings().all()
        seen = {r["id"] for r in rows}
        for r in relaxed:
            if r["id"] not in seen:
                rows.append(dict(r))
                seen.add(r["id"])
            if len(rows) >= limit:
                break
        return rows

    async def vector_search(self, embedding: list[float], limit: int, guests: list[str] | None = None) -> list[dict]:
        vec = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
        gf = "AND e.guest = ANY(:guests)" if guests else ""
        params: dict = {"vec": vec, "limit": limit}
        if guests:
            params["guests"] = guests
        rows = (
            await self.db.execute(
                text(
                    """
                    SELECT c.id, c.episode_id, c.chunk_index, c.speaker, c.start_seconds, c.end_seconds,
                           c.text, e.guest, e.title, e.youtube_url, e.video_id, e.publish_date,
                           1 - (c.embedding <=> CAST(:vec AS vector)) AS score
                    FROM chunks c JOIN episodes e ON e.id = c.episode_id
                    WHERE c.embedding IS NOT NULL AND NOT c.is_ad {gf}
                    ORDER BY c.embedding <=> CAST(:vec AS vector)
                    LIMIT :limit
                    """.replace("{gf}", gf)
                ),
                params,
            )
        ).mappings().all()
        return [dict(r) for r in rows]

    async def neighbours(self, chunk_id: int, span: int = 1) -> list[dict]:
        """Adjacent chunks, used to widen a citation's context window."""
        rows = (
            await self.db.execute(
                text(
                    """
                    SELECT n.id, n.chunk_index, n.speaker, n.start_seconds, n.text
                    FROM chunks c JOIN chunks n ON n.episode_id = c.episode_id
                    WHERE c.id = :id AND abs(n.chunk_index - c.chunk_index) <= :span
                    ORDER BY n.chunk_index
                    """
                ),
                {"id": chunk_id, "span": span},
            )
        ).mappings().all()
        return [dict(r) for r in rows]


def serialize_row(row: dict) -> dict:
    """Make DB rows JSON-safe (UUID/datetime → str)."""
    out = {}
    for k, v in row.items():
        if isinstance(v, (uuid.UUID,)):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out
