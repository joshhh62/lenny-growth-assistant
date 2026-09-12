-- Lenny Growth Assistant — PostgreSQL schema.
-- Idempotent: safe to run on every startup. Requires the pgvector extension
-- (bundled in the docker image; on Supabase enable it under Database → Extensions).

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Users & sessions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,                 -- client-generated anonymous id
    display_name  TEXT,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id            UUID PRIMARY KEY,
    user_id       TEXT REFERENCES users(id) ON DELETE SET NULL,
    title         TEXT NOT NULL DEFAULT 'New chat',
    provider      TEXT NOT NULL,                    -- 'anthropic' | 'ollama'
    model         TEXT NOT NULL,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb, -- user agent, locale, etc.
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_user_updated_idx ON sessions (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id            UUID PRIMARY KEY,
    session_id    UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role          TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content       TEXT NOT NULL,
    citations     JSONB NOT NULL DEFAULT '[]'::jsonb,
    tool_calls    JSONB NOT NULL DEFAULT '[]'::jsonb,
    provider      TEXT,
    model         TEXT,
    runtime       TEXT,
    latency_ms    INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_session_created_idx ON messages (session_id, created_at);

CREATE TABLE IF NOT EXISTS artifacts (
    id            UUID PRIMARY KEY,
    session_id    UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id    UUID REFERENCES messages(id) ON DELETE SET NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('markdown', 'html')),
    title         TEXT NOT NULL,
    content       TEXT NOT NULL,                    -- sanitized on write
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS artifacts_session_idx ON artifacts (session_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Knowledge base
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS episodes (
    id               TEXT PRIMARY KEY,              -- folder slug, e.g. 'wes-kao'
    guest            TEXT NOT NULL,
    title            TEXT NOT NULL,
    youtube_url      TEXT,
    video_id         TEXT,
    publish_date     DATE,
    duration_seconds INTEGER,
    keywords         JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_path      TEXT NOT NULL,                 -- path inside the transcripts repo
    content_hash     TEXT NOT NULL,                 -- sha256 of transcript.md → refresh detection
    chunk_count      INTEGER NOT NULL DEFAULT 0,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id             BIGSERIAL PRIMARY KEY,
    episode_id     TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    chunk_index    INTEGER NOT NULL,
    speaker        TEXT,
    start_seconds  INTEGER,
    end_seconds    INTEGER,
    text           TEXT NOT NULL,
    token_estimate INTEGER NOT NULL,
    -- Lexical index: always available, no model needed.
    tsv            tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    -- Semantic index: filled in progressively by the embedding worker (nullable).
    embedding      vector(768),
    UNIQUE (episode_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS chunks_episode_idx ON chunks (episode_id);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks
    USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL;

-- Ingestion runs, so the /health/retrieval endpoint can report freshness.
CREATE TABLE IF NOT EXISTS ingest_runs (
    id            BIGSERIAL PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    episodes_seen INTEGER NOT NULL DEFAULT 0,
    episodes_new  INTEGER NOT NULL DEFAULT 0,
    chunks_written INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'running',  -- running | ok | failed
    error         TEXT
);
