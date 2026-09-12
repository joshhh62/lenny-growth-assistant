"""HTTP routes."""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.events import EventStream, sse_format
from app.agent.service import ChatService
from app.config import get_settings
from app.db.database import db_health, get_db, get_sessionmaker
from app.db.repository import ConversationRepo, KnowledgeRepo, serialize_row
from app.llm import providers
from app.logging_setup import get_logger
from app.rag.ingest import run_ingest
from app.rag.retriever import Retriever
from app.schemas.api import (
    ArtifactOut, ArtifactSummary, ConfigOut, MessageCreate, MessageOut, ProviderInfo, SearchHit,
    SearchOut, SessionCreate, SessionDetail, SessionOut, SessionUpdate,
)

log = get_logger("api")
router = APIRouter()
DB = Annotated[AsyncSession, Depends(get_db)]


def _embedder(request: Request):
    return getattr(request.app.state, "embedder", None)


# --- health -----------------------------------------------------------------
@router.get("/health", tags=["health"])
async def health_live():
    """Liveness: the process is up."""
    return {"status": "ok", "app": get_settings().app_name}


@router.get("/health/ready", tags=["health"])
async def health_ready(request: Request):
    """Readiness: DB reachable + which providers are usable + index freshness."""
    db = await db_health()
    provs = await providers.check_all()
    worker = getattr(request.app.state, "embedding_worker", None)
    ready = db["ok"] and any(p.available for p in provs)
    return {
        "status": "ready" if ready else "degraded",
        "database": db,
        "providers": [p.to_dict() for p in provs],
        "embeddings": worker.state if worker else {"status": "disabled"},
        "default_provider": get_settings().llm_provider,
    }


@router.get("/health/providers", tags=["health"])
async def health_providers():
    return [p.to_dict() for p in await providers.check_all()]


# --- config -----------------------------------------------------------------
@router.get("/api/config", response_model=ConfigOut, tags=["config"])
async def get_config(db: DB):
    s = get_settings()
    provs = await providers.check_all()
    stats = await KnowledgeRepo(db).stats()
    return ConfigOut(
        app_name=s.app_name,
        default_provider=s.llm_provider,
        fallback_provider=s.llm_fallback_provider or None,
        providers=[ProviderInfo(**p.to_dict()) for p in provs],
        retrieval={**serialize_row(stats), "top_k": s.retrieval_top_k, "mode": "hybrid" if s.embeddings_enabled else "lexical"},
    )


# --- sessions ---------------------------------------------------------------
@router.post("/api/sessions", response_model=SessionOut, status_code=201, tags=["sessions"])
async def create_session(body: SessionCreate, db: DB, request: Request):
    s = get_settings()
    repo = ConversationRepo(db)
    if body.user_id:
        await repo.upsert_user(body.user_id, body.display_name, body.metadata)
    provider = body.provider or s.llm_provider
    meta = {**body.metadata, "user_agent": request.headers.get("user-agent", "")[:200]}
    row = await repo.create_session(
        user_id=body.user_id, provider=provider, model=s.model_for(provider),
        title=body.title or "New chat", metadata=meta,
    )
    return SessionOut(**row)


@router.get("/api/sessions", response_model=list[SessionOut], tags=["sessions"])
async def list_sessions(db: DB, user_id: str | None = None, limit: int = Query(50, le=200)):
    return [SessionOut(**r) for r in await ConversationRepo(db).list_sessions(user_id, limit)]


@router.get("/api/sessions/{session_id}", response_model=SessionDetail, tags=["sessions"])
async def get_session(session_id: uuid.UUID, db: DB):
    repo = ConversationRepo(db)
    sess = await repo.get_session(session_id)
    if not sess:
        raise HTTPException(404, detail={"code": "session_not_found", "message": "Session not found"})
    msgs = await repo.list_messages(session_id)
    arts = await repo.list_artifacts(session_id)
    return SessionDetail(
        session=SessionOut(**sess),
        messages=[MessageOut(**m) for m in msgs],
        artifacts=[ArtifactSummary(**a) for a in arts],
    )


@router.patch("/api/sessions/{session_id}", response_model=SessionOut, tags=["sessions"])
async def update_session(session_id: uuid.UUID, body: SessionUpdate, db: DB):
    s = get_settings()
    fields = body.model_dump(exclude_none=True)
    if "provider" in fields:
        fields["model"] = s.model_for(fields["provider"])
    row = await ConversationRepo(db).update_session(session_id, **fields)
    if not row:
        raise HTTPException(404, detail={"code": "session_not_found", "message": "Session not found"})
    return SessionOut(**row)


@router.delete("/api/sessions/{session_id}", status_code=204, tags=["sessions"])
async def delete_session(session_id: uuid.UUID, db: DB):
    if not await ConversationRepo(db).delete_session(session_id):
        raise HTTPException(404, detail={"code": "session_not_found", "message": "Session not found"})


# --- chat (SSE) -------------------------------------------------------------
@router.post("/api/sessions/{session_id}/messages", tags=["chat"])
async def post_message(session_id: uuid.UUID, body: MessageCreate, request: Request):
    """Send a user message; the reply streams back as Server-Sent Events.

    Events: provider, status, token, tool_call, tool_result, citations, artifact, done, error.
    """
    sm = get_sessionmaker()
    async with sm() as db:
        sess = await ConversationRepo(db).get_session(session_id)
    if not sess:
        raise HTTPException(404, detail={"code": "session_not_found", "message": "Session not found"})

    events = EventStream()

    async def run() -> None:
        try:
            async with sm() as db:
                service = ChatService(db, _embedder(request))
                await service.handle(sess, body.content, events)
        except Exception as exc:  # noqa: BLE001
            log.exception("chat_task_failed")
            await events.emit("error", code="internal", message=f"{type(exc).__name__}: {str(exc)[:200]}", retryable=True)
        finally:
            await events.close()

    task = asyncio.create_task(run())

    async def stream():
        try:
            async for ev in events:
                yield sse_format(ev)
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- artifacts --------------------------------------------------------------
@router.get("/api/artifacts/{artifact_id}", response_model=ArtifactOut, tags=["artifacts"])
async def get_artifact(artifact_id: uuid.UUID, db: DB):
    row = await ConversationRepo(db).get_artifact(artifact_id)
    if not row:
        raise HTTPException(404, detail={"code": "artifact_not_found", "message": "Artifact not found"})
    return ArtifactOut(**row)


# --- retrieval (debug/eval) -------------------------------------------------
@router.get("/api/search", response_model=SearchOut, tags=["retrieval"])
async def search(db: DB, request: Request, q: str = Query(..., min_length=2, max_length=500), k: int = Query(6, ge=1, le=20)):
    retriever = Retriever(KnowledgeRepo(db), _embedder(request))
    hits, diag = await retriever.search(q, top_k=k)
    return SearchOut(
        query=q, mode=diag["mode"], duration_ms=diag["duration_ms"],
        hits=[SearchHit(n=i, **{k_: v for k_, v in h.to_dict().items() if k_ in SearchHit.model_fields}) for i, h in enumerate(hits, 1)],
    )


# --- admin: ingestion -------------------------------------------------------
@router.post("/api/admin/ingest", status_code=202, tags=["admin"])
async def trigger_ingest(background: BackgroundTasks, limit: int | None = None):
    """Kick off an incremental ingest in the background. Idempotent."""
    async def _job():
        try:
            await run_ingest(get_sessionmaker(), limit=limit)
        except Exception:  # noqa: BLE001
            log.exception("background_ingest_failed")
    background.add_task(_job)
    return {"status": "started"}


@router.get("/api/admin/ingest/status", tags=["admin"])
async def ingest_status(db: DB, request: Request):
    repo = KnowledgeRepo(db)
    worker = getattr(request.app.state, "embedding_worker", None)
    return {
        "stats": serialize_row(await repo.stats()),
        "last_run": serialize_row(await repo.last_ingest_run() or {}),
        "embeddings": worker.state if worker else {"status": "disabled"},
    }
