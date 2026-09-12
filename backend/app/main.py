"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import get_settings
from app.db.database import DatabaseUnavailable, apply_schema, db_health, dispose, get_sessionmaker, wait_for_db
from app.logging_setup import RequestContextMiddleware, configure_logging, get_logger, request_id_var
from app.rag.embeddings import EmbeddingWorker, OllamaEmbedder
from app.rag.ingest import run_ingest

log = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    configure_logging(s.log_level, s.log_format)
    log.info("startup", env=s.app_env, provider=s.llm_provider, fallback=s.llm_fallback_provider or None,
             runtime=s.agent_runtime, ollama=s.ollama_base_url, model_local=s.ollama_model, model_cloud=s.anthropic_model)
    app.state.db_ready = False
    app.state.embedder = None
    app.state.embedding_worker = None

    if await wait_for_db():
        await apply_schema()
        app.state.db_ready = True
    else:
        # Start anyway so /health reports the problem instead of the container crash-looping.
        log.error("db_unavailable_at_startup", database_url=s.database_url.split("@")[-1])

    if app.state.db_ready and s.auto_ingest_on_start:
        stats = await db_health()
        if stats.get("ok") and stats.get("episodes", 0) == 0:
            log.info("auto_ingest_scheduled", reason="knowledge base is empty")

            async def _first_ingest() -> None:
                try:
                    await run_ingest(get_sessionmaker())
                except Exception:  # noqa: BLE001
                    log.exception("auto_ingest_failed")

            asyncio.create_task(_first_ingest(), name="auto-ingest")

    if s.embeddings_enabled:
        app.state.embedder = OllamaEmbedder()
        if app.state.db_ready:
            app.state.embedding_worker = EmbeddingWorker(get_sessionmaker(), app.state.embedder)
            app.state.embedding_worker.start()
    try:
        yield
    finally:
        if app.state.embedding_worker:
            await app.state.embedding_worker.stop()
        if app.state.embedder:
            try:
                await asyncio.wait_for(app.state.embedder.aclose(), timeout=3)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(dispose(), timeout=5)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            log.warning("db_dispose_timeout")
        log.info("shutdown")


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title=s.app_name,
        version="1.0.0",
        description="Grounded product & growth assistant over Lenny's Podcast transcripts.",
        lifespan=lifespan,
        docs_url="/docs",
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware, allow_origins=s.cors_origin_list, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"], expose_headers=["x-request-id"],
    )
    app.include_router(router)

    # --- structured errors ---------------------------------------------------
    def _err(status: int, code: str, message: str, details=None) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={"error": {"code": code, "message": message, "details": details,
                               "request_id": request_id_var.get()}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return _err(422, "validation_error", "Request failed validation", exc.errors())

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException):
        if isinstance(exc.detail, dict):
            return _err(exc.status_code, exc.detail.get("code", "http_error"), exc.detail.get("message", ""), exc.detail.get("details"))
        return _err(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(DatabaseUnavailable)
    async def _db(_: Request, exc: DatabaseUnavailable):
        log.error("database_unavailable", error=str(exc)[:200])
        return _err(503, "database_unavailable", "The database is unreachable. Check DATABASE_URL and that Postgres is running.")

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled_exception")
        return _err(500, "internal_error", "Unexpected server error")

    return app


app = create_app()
