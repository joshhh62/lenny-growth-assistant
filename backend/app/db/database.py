"""Database engine, startup schema application and health check."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger("db")

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


class DatabaseUnavailable(RuntimeError):
    """Raised when Postgres cannot be reached; mapped to HTTP 503 by the API."""


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(
            s.database_url,
            pool_size=s.db_pool_size,
            pool_pre_ping=True,
            connect_args={"timeout": s.db_connect_timeout_s},
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_db():
    """FastAPI dependency yielding a transactional session."""
    try:
        async with get_sessionmaker()() as session:
            yield session
            await session.commit()
    except (OSError, asyncio.TimeoutError, OperationalError, InterfaceError) as exc:  # refused / timed out
        raise DatabaseUnavailable(str(exc)) from exc


async def apply_schema() -> None:
    raw = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    # Strip `-- comments` before splitting on ';' (comments may contain semicolons).
    sql = "\n".join(line.split("--", 1)[0] for line in raw.splitlines())
    engine = get_engine()
    async with engine.begin() as conn:
        # Execute statement by statement so a partial failure is diagnosable.
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            await conn.execute(text(stmt))
    log.info("schema_applied")


async def wait_for_db(retries: int = 20, delay_s: float = 1.5) -> bool:
    """Retry connecting on startup (Postgres in compose may still be booting)."""
    for attempt in range(1, retries + 1):
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("db_not_ready", attempt=attempt, error=str(exc)[:200])
            await asyncio.sleep(delay_s)
    return False


async def db_health() -> dict:
    try:
        async with get_engine().connect() as conn:
            row = (await conn.execute(text(
                "SELECT (SELECT count(*) FROM episodes) AS episodes,"
                " (SELECT count(*) FROM chunks) AS chunks,"
                " (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) AS embedded"
            ))).mappings().one()
        return {"ok": True, **dict(row)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}


async def dispose() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
