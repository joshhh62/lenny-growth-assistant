"""Test fixtures.

Unit tests need nothing. Integration tests (marked `integration`) need Postgres
with pgvector; they use `TEST_DATABASE_URL` (default: a `lenny_test` database
on the same server as DATABASE_URL, created on demand). The LLM is always the
in-process fake from `tests/fake_llm.py` — no keys, no network, no Ollama.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from tests.fake_llm import FakeLLMServer

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: needs Postgres")


@pytest.fixture(scope="session")
def fake_llm():
    srv = FakeLLMServer().start()
    yield srv
    srv.stop()


@pytest.fixture(scope="session")
def test_env(fake_llm):
    """Point the app at the fake LLM and a test database *before* settings load."""
    base = os.environ.get("DATABASE_URL", "postgresql+asyncpg://lenny:lenny@localhost:5432/lenny")
    test_url = os.environ.get("TEST_DATABASE_URL") or base.rsplit("/", 1)[0] + "/lenny_test"
    env = {
        "DATABASE_URL": test_url,
        "TRANSCRIPTS_DIR": str(FIXTURES / "transcripts"),
        "LLM_PROVIDER": "ollama",
        "LLM_FALLBACK_PROVIDER": "",
        "OLLAMA_BASE_URL": fake_llm.url,
        "OLLAMA_MODEL": "fake-model",
        "OLLAMA_EMBED_MODEL": "nomic-embed-text",
        "EMBEDDINGS_ENABLED": "true",
        "ANTHROPIC_API_KEY": "",
        "AGENT_RUNTIME": "messages_loop",
        "OLLAMA_TOOLS_ENABLED": "true",  # exercise the tool loop against the fake
        "LOG_FORMAT": "console",
        "LOG_LEVEL": "WARNING",
        "APP_ENV": "test",
        "LLM_TIMEOUT_S": "5",
        "CHUNK_TARGET_TOKENS": "80",  # small fixtures → several chunks per episode
    }
    os.environ.update(env)
    from app.config import get_settings

    get_settings.cache_clear()
    return env


async def _ensure_test_db(test_url: str) -> bool:
    """Create the test database if missing. Returns False if Postgres is unreachable."""
    import asyncpg

    admin_url = test_url.replace("postgresql+asyncpg://", "postgresql://")
    dbname = admin_url.rsplit("/", 1)[1]
    base = admin_url.rsplit("/", 1)[0] + "/postgres"
    try:
        conn = await asyncpg.connect(base, timeout=3)
    except Exception:  # noqa: BLE001
        return False
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", dbname)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await conn.close()
    conn = await asyncpg.connect(admin_url)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await conn.close()
    return True


@pytest.fixture(scope="session")
def db_available(test_env):
    ok = asyncio.run(_ensure_test_db(test_env["DATABASE_URL"]))
    if not ok:
        pytest.skip("Postgres not reachable; skipping integration tests")
    return ok


@pytest.fixture()
async def client(db_available, test_env):
    """Async HTTP client against a fresh app with a clean, fixture-ingested DB."""
    import httpx
    from sqlalchemy import text

    from app.db import database
    from app.db.database import apply_schema, get_engine
    from app.main import create_app
    from app.rag.ingest import run_ingest

    database._engine = None  # fresh engine per test (new event loop)
    database._sessionmaker = None
    await apply_schema()
    async with get_engine().begin() as conn:
        # A previous test's background worker may still hold a connection; kill it
        # rather than letting TRUNCATE wait on its lock forever.
        await conn.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ))
        await conn.execute(text("SET lock_timeout = '10s'"))
        await conn.execute(text("TRUNCATE users, sessions, messages, artifacts, episodes, chunks, ingest_runs RESTART IDENTITY CASCADE"))
    await run_ingest(database.get_sessionmaker())

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    await database.dispose()
