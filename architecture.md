# Architecture — The Lenny Growth Assistant

This document is the map a client engineer needs to run, debug, and extend the system.
Where a decision was a trade-off, the alternative and the reason are stated.

## 1. System overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  Browser  (React + Vite, served by nginx in Docker or Vite in dev)            │
│  ┌────────────┐  ┌──────────────────────┐  ┌──────────────────────────────┐  │
│  │ Sessions   │  │ Chat (SSE stream,    │  │ Artifact viewer               │  │
│  │ + provider │  │ inline [n] citations)│  │ <iframe sandbox=""> / Markdown│  │
│  └────────────┘  └──────────┬───────────┘  └──────────────────────────────┘  │
└─────────────────────────────┼────────────────────────────────────────────────┘
                              │ JSON + text/event-stream          (same origin via nginx)
┌─────────────────────────────▼────────────────────────────────────────────────┐
│  FastAPI  (backend/app)                                                       │
│  api/routes ─► agent/service (orchestration)                                  │
│                   ├─ llm/providers   resolve provider + fallback              │
│                   ├─ agent/router    deterministic intent → chat/essay/artifact│
│                   ├─ rag/retriever   hybrid FTS + pgvector, RRF fusion        │
│                   ├─ agent/runtime   MessagesLoop  │  AgentSDK                │
│                   │                    (Anthropic Messages API, tool calling)  │
│                   ├─ agent/tools     search_transcripts · write_ship30_essay ·│
│                   │                  create_artifact  (one registry, 2 runtimes)│
│                   ├─ skills/ship30   SKILL.md + essay pipeline + checks       │
│                   └─ artifacts/      sanitize (bleach allow-list + CSP)       │
│  rag/ingest (parse → chunk → upsert) · rag/embeddings (background worker)     │
└───────────┬──────────────────────────────────┬───────────────────────────────┘
            │ asyncpg                          │ HTTP (Anthropic Messages API shape)
┌───────────▼───────────┐        ┌─────────────▼───────────┐   ┌────────────────┐
│ PostgreSQL 16         │        │ Ollama (host machine)    │   │ Anthropic API   │
│ + pgvector            │        │ /v1/messages, /api/embed │   │ (optional)      │
│ sessions, messages,   │        │ qwen2.5:7b               │   │ claude-sonnet   │
│ artifacts, episodes,  │        │ nomic-embed-text         │   └────────────────┘
│ chunks (tsv + vector) │        └─────────────────────────┘
└───────────────────────┘
```

**One idea runs through the design:** every dependency other than Postgres is optional at runtime. No Ollama → lexical retrieval still works and cloud can answer. No cloud key → local answers. Ollama down mid-session → typed error or fallback, never a crash.

## 2. Component boundaries

| Component | Responsibility | Knows about | Does **not** know about |
|---|---|---|---|
| `api/routes.py` | HTTP contracts, validation, SSE framing | `ChatService`, repos | Models, prompts |
| `agent/service.py` | One user turn: provider → route → retrieve → run → persist | Everything below | HTTP |
| `agent/router.py` | Pure function `message → Intent` | Regexes | Models, DB |
| `agent/runtime.py` | Drive a model loop; execute tool calls; stream tokens | Anthropic client / Agent SDK, tool registry | DB schema, HTTP |
| `agent/tools.py` | Tool schemas + handlers, shared by both runtimes | Retriever, artifact store, essay skill | Which runtime called them |
| `skills/ship30/` | `SKILL.md` (principles) + `essay.py` (retrieve → generate → check → revise) | Retriever, a `generate(system, user)` callable | Runtime, HTTP |
| `rag/ingest.py` | Files → episodes → chunks | Filesystem, `KnowledgeRepo` | Models |
| `rag/retriever.py` | Query → citations | `KnowledgeRepo`, embedder | Prompts |
| `rag/embeddings.py` | Ollama embed client + background worker | Ollama HTTP, `KnowledgeRepo` | Chat |
| `artifacts/sanitize.py` | Untrusted HTML/Markdown → safe document | bleach | Everything else |
| `llm/providers.py` | Health checks, client factory, fallback resolution | Settings, Ollama/Anthropic endpoints | Prompts, DB |
| `db/repository.py` | All SQL | SQLAlchemy Core | Business logic |

The boundary that matters most: **tools are the only way the model touches the world.** Retrieval, the essay skill and artifact creation are tool handlers; the runtimes only differ in *who drives the loop*.

## 3. Database schema

PostgreSQL 16 with `pgvector`. Applied idempotently at startup from [`backend/app/db/schema.sql`](backend/app/db/schema.sql).

```
users            id TEXT PK · display_name · metadata JSONB · created_at · last_seen_at
sessions         id UUID PK · user_id FK→users · title · provider · model · metadata JSONB · created_at · updated_at
messages         id UUID PK · session_id FK→sessions (CASCADE) · role · content · citations JSONB · tool_calls JSONB
                 · provider · model · runtime · latency_ms · input_tokens · output_tokens · error · created_at
artifacts        id UUID PK · session_id FK · message_id FK · kind ('markdown'|'html') · title · content (sanitized) · created_at
episodes         id TEXT PK (folder slug) · guest · title · youtube_url · video_id · publish_date · duration_seconds
                 · keywords JSONB · source_path · content_hash · chunk_count · ingested_at
chunks           id BIGSERIAL PK · episode_id FK (CASCADE) · chunk_index · speaker · start_seconds · end_seconds
                 · text · token_estimate · tsv tsvector GENERATED (GIN) · embedding vector(768) NULL (HNSW, partial)
ingest_runs      id · started_at · finished_at · episodes_seen · episodes_new · chunks_written · status · error
```

Design notes:
- `messages.citations` stores the *resolved* citations (guest, title, timestamp URL, passage text) so history renders without re-querying and survives re-ingests.
- `chunks.embedding` is nullable with a **partial HNSW index** (`WHERE embedding IS NOT NULL`): the lexical index is available the moment ingest finishes; vectors fill in progressively.
- `episodes.content_hash` = sha256(parser version + file). Re-running ingest is an incremental refresh; bumping `PARSER_VERSION` forces a rebuild.
- Everything cascades from `sessions`, so deleting a conversation removes its messages and artifacts.

## 4. API

Base URL `http://localhost:8000`. OpenAPI at `/docs`. All errors share one envelope:

```json
{ "error": { "code": "session_not_found", "message": "Session not found", "details": null, "request_id": "a1b2c3d4e5f6" } }
```

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/health` | Liveness | Always 200 if the process is up |
| GET | `/health/ready` | Readiness: DB, providers (with reasons), embedding progress | `status: ready|degraded` |
| GET | `/health/providers` | Provider health only | Used by the UI toggle |
| GET | `/api/config` | Default/fallback provider, model names, retrieval stats | UI bootstrap |
| POST | `/api/sessions` | Create session `{user_id?, display_name?, provider?, title?, metadata?}` | 201; stores user metadata + user agent |
| GET | `/api/sessions?user_id=` | List sessions for a user | newest first |
| GET | `/api/sessions/{id}` | Session + messages + artifact summaries | |
| PATCH | `/api/sessions/{id}` | Rename or switch provider | model is set from provider |
| DELETE | `/api/sessions/{id}` | Delete (cascades) | 204 |
| POST | `/api/sessions/{id}/messages` | Send `{content}`; **SSE** response | see events below |
| GET | `/api/artifacts/{id}` | Sanitized artifact content | |
| GET | `/api/search?q=&k=` | Retrieval debug: hits, mode, timing | used by the smoke test |
| POST | `/api/admin/ingest?limit=` | Incremental ingest in the background | 202 |
| GET | `/api/admin/ingest/status` | Stats, last run, embedding worker state | |

**SSE events** (`event:` name = `type` field):
`provider` → `status`* → (`tool_call` → `tool_result`)* → `citations`* → `token`* → `artifact`? → `citations(final)` → `done` | `error`

`error` carries `{code, message, retryable}` with codes: `no_provider`, `auth`, `timeout`, `unreachable`, `rate_limited`, `model_not_found`, `provider_error`, `agent_sdk`, `internal`. The same code is persisted on the assistant message's `error` column so history explains itself.

## 5. Ingestion and retrieval

### Ingestion (`rag/ingest.py`)
1. **Locate** `TRANSCRIPTS_DIR`; if empty, `git clone --depth 1` the ChatPRD repo (first boot only; the volume persists).
2. **Parse** each `episodes/<slug>/transcript.md`: YAML frontmatter + body. Three body formats exist in the corpus and all are supported:
   `Speaker (00:12:34):` / `(12:34):` continuation lines, `Speaker:` with no timestamps, and `[00:12:34] Speaker: text` inline.
3. **Dedupe**: 33 `video_id`s appear under two folders (a source-repo defect). The folder whose slug matches the title-derived guest wins; the other is dropped. `guest` is taken from the title suffix when it looks like a person's name, else frontmatter with "N.0" version suffixes stripped. Result: **303 folders → 268 episodes**.
4. **Chunk** by speaker turn: consecutive turns are packed to ≈350 tokens with a one-turn overlap; over-long turns split on sentence boundaries. Each chunk keeps `speaker`, `start_seconds`, `end_seconds` → **27,924 chunks**.
5. **Upsert** episodes + chunks; `tsv` is a generated column so lexical search is live immediately. Ingest of the full corpus takes ~15 s.
6. **Embed** in the background (`EmbeddingWorker`): batches of 32 through Ollama's `/api/embed` (`nomic-embed-text`, with `search_document:` / `search_query:` prefixes). Pauses with backoff when Ollama is unreachable; resumes automatically. ~20–40 min for the full corpus on a CPU laptop; retrieval is hybrid for whatever is embedded so far.

**Refresh**: `POST /api/admin/ingest` (or `make ingest`) re-reads the folder; unchanged files are skipped by hash. To pick up new episodes, `git pull` in the transcripts volume (or delete the volume) and re-run.

**Traceability**: every chunk → `episode_id` → `source_path` in the repo, plus `video_id` + `start_seconds` → `https://www.youtube.com/watch?v=<id>&t=<s>s`.

### Retrieval (`rag/retriever.py`)
```
query ──► [embed via Ollama (8 s timeout)] ──► pgvector cosine top-24 ──► drop < 0.45 similarity ─┐
      └─► websearch_to_tsquery (AND) top-24; if under-filled, OR over terms ────────────────────┤
                                                                                                ▼
                                       Reciprocal Rank Fusion (k=60) ──► diversify (≤2 chunks/episode,
                                       no adjacent chunks) ──► top-k citations + diagnostics
```
- Lexical uses `ts_rank_cd` (cover density) which rewards term proximity, plus a boost when the episode *title* matches the query, so an episode about the topic outranks a passing mention.
- Sponsor reads and housekeeping segments (~5 % of chunks, e.g. "This episode is brought to you by…") are flagged `is_ad` at ingest and excluded from both indexes.
- The similarity floor is what makes "the transcripts don't cover this" possible: without it, nearest-neighbour search always returns *something*.
- Diagnostics (`mode`, counts, timings, vector errors) are logged per query as `retrieval` events.

## 6. Agent layer

### Routing (`agent/router.py`)
A pure, unit-tested function classifies each message into `chat`, `essay`, `artifact_markdown`, `artifact_html`, extracting a topic and a Ship 30 "4A" angle. Skills are routed **deterministically** because a 7B model cannot be trusted to call a tool with a 1,200-word argument; free-form questions go to the model, which may call `search_transcripts` itself for follow-ups. Referential topics ("turn *that* into an essay") resolve to the previous user question.

### Runtimes (`agent/runtime.py`) — the key trade-off
| | `MessagesLoopRuntime` | `AgentSDKRuntime` |
|---|---|---|
| What | ~80-line tool-calling loop on the Anthropic Messages API (streaming) | `claude_agent_sdk.query()` with our tools exposed as an in-process MCP server |
| Works with | Anthropic **and** Ollama (`/v1/messages` Anthropic-compatible endpoint) | Anthropic; Ollama via `ANTHROPIC_BASE_URL` (supported, but see below) |
| Default for | `ollama` | `anthropic` |
| Why | Minimal prompt overhead — matters when a CPU model processes ~50 tokens/s | Production agent loop, hooks, budgets, session tooling — the brief's named SDK, used where its strengths apply |
| Cost | We own the loop (tested) | Subprocess with its own system prompt; on a CPU model that overhead alone can add tens of seconds per turn |

Both runtimes consume the **same tool registry** (`agent/tools.py`) — schema and handler defined once; the SDK runtime wraps handlers with `@tool` + `create_sdk_mcp_server`. `AGENT_RUNTIME=agent_sdk|messages_loop|auto` overrides the default for evaluation. The SDK path is covered by an integration test that drives the real SDK subprocess against the fake Messages API (`test_agent_sdk_runtime_calls_mcp_tool_and_streams`).

**Local-model budget.** On a CPU model every tool round re-processes the full prompt, so free-form chat on Ollama does not expose tools by default (`OLLAMA_TOOLS_ENABLED=false`): grounding comes from retrieve-then-generate, follow-ups expand the retrieval query with the previous question, and skills are routed deterministically anyway. Output is capped (`OLLAMA_MAX_OUTPUT_TOKENS=800`), essays use 8 passages instead of 10 and skip the revision pass unless the draft is badly off, and readiness warns if the loaded model's context window is below 8k (`/api/ps`).

### Grounding strategy
*Retrieve-then-generate on every turn.* The service pre-fetches passages for the new message and injects them as a numbered CONTEXT block, so even a model that never calls tools is grounded. The model may call `search_transcripts` for more; new passages get stable global numbers. After the turn, only citations referenced as `[n]` in the answer are kept (fallback: top 3 consulted). Retrieved passages are treated as data; the system prompt forbids following instructions inside them.

### Ship 30 for 30 skill (`skills/ship30/`)
`SKILL.md` encodes the guide's principles as a contract (specificity + 4A angle, WHO/WHAT/WHY headline with a curiosity gap, hook, wheels-and-spokes skimmability, 1/3/1 rhythm, rate of revelation, Tequila Test differentiation, grounding rules, "Do this next" ending, output format, checklist). `essay.py` runs three retrieval queries per topic, generates with the skill as the system prompt, validates programmatically (word count, H1/H2 counts, ≥5 citations that exist, bullets, bold, ending section, weak-opener check), and performs one revision pass with the failed checks listed. The Sources block is appended by code, never by the model.

## 7. Model toggle and fallback

```
LLM_PROVIDER=ollama|anthropic          default; UI can override per session (sessions.provider)
LLM_FALLBACK_PROVIDER=anthropic|ollama|""
AGENT_RUNTIME=auto|agent_sdk|messages_loop
ANTHROPIC_API_KEY / ANTHROPIC_MODEL    OLLAMA_BASE_URL / OLLAMA_MODEL / OLLAMA_EMBED_MODEL
```
Resolution per turn (`llm/providers.resolve`): health-check the requested provider (key present & well-formed; Ollama reachable **and** model pulled). If unhealthy and a fallback is configured and healthy → use it and emit `provider{fallback_used:true, reason}` so the UI shows *· fallback* and a toast. If neither is usable → `error{code:"no_provider"}` with both reasons. The provider, model and runtime are recorded on every assistant message.

## 8. Security

**Artifacts are untrusted.** Two independent layers:

| Layer | Where | Permits | Blocks |
|---|---|---|---|
| **1. Sanitize on write** (`artifacts/sanitize.py`) | Server, before storing | Document/structure tags, lists, tables, inline SVG shapes, `<style>` with cleaned CSS, `class/id/style/aria-*`, `https:` links, **inline base64 images only** | `<script>`, `<iframe>`, `<object>`, `<embed>`, `<form>`, inputs, `<link>`, `<meta>`, `<base>`, all `on*` handlers, `javascript:`/`data:` links, remote image/CSS URLs, `@import`, `expression()`, `url()`, `behavior:`; oversize content is truncated |
| **2. Isolate on render** (`ArtifactPanel.tsx`) | Browser | Layout + styles | `<iframe sandbox="">` — opaque origin, no scripts, forms, popups, top navigation or parent access; plus an injected CSP: `default-src 'none'; style-src 'unsafe-inline'; img-src data:` |

Markdown artifacts and chat messages render through `react-markdown` with raw HTML disabled. The sanitizer logs what it removed (`artifact_sanitized` events) so an unusual model output is visible to operators.

Other controls: secrets only via environment (`.env` git-ignored, `.env.example` clean); CORS restricted to the UI origins; Pydantic validation at the edge (message ≤ 8,000 chars, provider enum); artifact size cap; SQL via bound parameters only; the API runs as a non-root user in Docker.

## 9. Observability

Structured JSON logs (structlog) with `request_id`, `session_id` and `component` on every line, so one failing conversation can be followed across layers:

| Event | Component | Diagnoses |
|---|---|---|
| `request` (method, path, status, duration_ms) | http | API errors, slow endpoints |
| `provider_fallback`, `turn_error{code}` | llm / chat | Model & key problems |
| `routed{intent, runtime, provider}` → `turn_complete{latency_ms, citations, artifacts, rounds, tokens}` | chat | Per-turn cost/latency |
| `retrieval{mode, lexical, vector, vector_filtered, duration_ms, vector_error}` | retrieval | Empty results, Ollama embed failures |
| `tool_result{tool, summary}`, `tool_failed` | tools | Skill failures |
| `essay_draft{word_count, problems}`, `essay_revised` | skill.ship30 | Quality-check outcomes |
| `artifact_sanitized{removed_tags, stripped_urls}` | tools | Unsafe model output |
| `embedding_progress` / `embedding_paused` | embeddings | Index build state |
| `db_not_ready`, `database_unavailable`, `schema_applied` | db | Postgres |

`/health/ready` exposes the same signals as JSON for dashboards or a `curl` (`make status`). `x-request-id` is returned on every response.

## 10. Deployment topology

```
docker compose up --build
  db   pgvector/pgvector:pg16   :5432   volume pgdata
  api  backend/Dockerfile       :8000   volume transcripts (cloned once)   → host.docker.internal:11434 (Ollama)
  web  frontend/Dockerfile      :3000   nginx: static UI + reverse proxy /api, /health, /docs → api
  ollama (profile ollama-in-docker, optional)  :11434
```
- The browser talks only to `web` (one origin, no CORS in production). SSE passes through nginx with buffering off.
- Ollama runs on the host by default because that is where the CPU/GPU is; Docker Desktop resolves `host.docker.internal`, Linux gets it via `extra_hosts`. Host Ollama must listen on all interfaces (`OLLAMA_HOST=0.0.0.0`) — the README's troubleshooting table covers this.
- Managed Postgres (Supabase/Railway): set `DATABASE_URL` and enable the `vector` extension; nothing else changes.
- No-Docker path: `uvicorn` + `npm run dev` with Vite proxying `/api` — identical code paths.

## 11. Extending the system

| You want to… | Change |
|---|---|
| Add a provider (OpenAI) | Implement `check_openai()` + a client in `llm/providers.py`; add to `Provider` literal; the runtime already speaks tool calling |
| Add a skill | Write `skills/<name>/SKILL.md` + a handler; register a `ToolSpec` in `agent/tools.py`; add a router rule if it should be deterministic |
| Change chunking | `rag/ingest.py`; bump `PARSER_VERSION`; `POST /api/admin/ingest` rebuilds |
| Swap embeddings model | `OLLAMA_EMBED_MODEL` + `embedding_dim` (schema column is `vector(768)`; change both) |
| Add auth | A FastAPI dependency that sets `user_id` from a token; all queries already scope by it |
| Tighten/loosen the artifact allow-list | `ALLOWED_TAGS` / `ALLOWED_ATTRS` in `artifacts/sanitize.py`; tests in `test_units.py` |
