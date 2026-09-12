# The Lenny Growth Assistant

A grounded, conversational assistant over **Lenny's Podcast transcripts** for product and growth teams. Ask a question and get an answer that cites the episode *and the minute*; ask for a Ship 30 for 30 essay or an HTML one-pager and it renders beside the chat. Runs fully local on a laptop with Ollama, or on Claude with one env var.

> Take-home submission for the Forward Deployed Engineer role at Oogway Labs — Joshith Sai Panchumarthi.
> Docs: **[PRD](PRD.md)** · **[architecture.md](architecture.md)** · **[design.md](design.md)** · [manual test plan](docs/manual-test-plan.md) · [agent transcripts](agent-transcripts/)

<p align="center"><img src="docs/screenshot-desktop.png" alt="Three-pane UI: sessions, grounded chat with citations, sandboxed artifact viewer" width="900"></p>

## What it does

| Capability | How |
|---|---|
| **Grounded Q&A** with follow-ups | Hybrid retrieval (Postgres full-text + pgvector) over 272 episodes / 27.9k timestamped chunks; every claim carries a `[n]` citation that deep-links to YouTube at that second. Says so when the transcripts don't cover a question. |
| **Local or cloud model, switchable in the UI** | Ollama (`qwen2.5:7b`, default, zero keys) or Anthropic Claude. Documented fallback when one is down. |
| **Ship 30 for 30 essay skill** | Principles encoded in [`SKILL.md`](backend/app/skills/ship30/SKILL.md); ~1,250 words; programmatic quality checks + revision pass. |
| **Artifacts** | Markdown docs and complete HTML/CSS pages, sanitized server-side and rendered in a sandboxed viewer next to the chat. |
| **Operable** | `docker compose up`, structured JSON logs, `/health/ready` that names what's wrong, typed errors, 45 automated tests + a smoke script. |

## Architecture in one paragraph

FastAPI serves a JSON/SSE API backed by PostgreSQL (+pgvector). A deterministic router sends each message to grounded chat, the essay skill, or artifact generation. One tool registry (`search_transcripts`, `write_ship30_essay`, `create_artifact`) is driven by either of two runtimes: the **Claude Agent SDK** (cloud) or a lean **Messages-API loop** (local) — both speak the Anthropic Messages format, which Ollama also exposes, so switching provider is pure config. The React UI streams tokens, shows citations and provenance, and renders artifacts in an `<iframe sandbox="">` with a CSP. Details and trade-offs: [architecture.md](architecture.md).

## Prerequisites

| | Required | Notes |
|---|---|---|
| **Docker Desktop** (or Docker Engine + Compose v2) | ✅ | Windows: enable WSL 2 when prompted. |
| **Ollama** | ✅ for the local model | [ollama.com/download](https://ollama.com/download). ~5 GB of models (below). 16 GB RAM recommended for `qwen2.5:7b`; use `qwen2.5:3b` on 8 GB. |
| Anthropic API key | optional | Enables the **Cloud** toggle. |
| Git | ✅ | The API clones the transcript repo on first boot (~28 MB). |
| Python 3.11 / Node 22 | only for the no-Docker path or running tests locally | |

## Quick start (Docker, one command)

```bash
git clone https://github.com/joshithsaip/lenny-growth-assistant.git
cd lenny-growth-assistant

# 1. Local models (one-time, ~5 GB)
ollama pull qwen2.5:7b
ollama pull nomic-embed-text

# 2. Let Docker reach Ollama on your machine (one-time)
#    Windows: set a user environment variable OLLAMA_HOST=0.0.0.0, then quit & relaunch Ollama from the tray.
#    macOS:   launchctl setenv OLLAMA_HOST 0.0.0.0 && restart Ollama
#    Linux:   OLLAMA_HOST=0.0.0.0 ollama serve

# 3. Configure (defaults are fine; add ANTHROPIC_API_KEY if you want Cloud)
cp .env.example .env

# 4. Run
docker compose up --build
```

Open **http://localhost:3000**. On first boot the API clones the transcripts and ingests them (~1 minute; watch `docker compose logs -f api` for `ingest_complete`). Full-text retrieval works immediately; semantic embeddings fill in over the next ~30 minutes in the background and retrieval upgrades itself to hybrid as they land.

Verify: `scripts/smoke.sh` (or `make status`).

API docs: http://localhost:8000/docs · Readiness: http://localhost:8000/health/ready

## Configuration

All configuration is environment variables — see [`.env.example`](.env.example) for every option with comments. The ones that matter:

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | Default provider: `ollama` or `anthropic`. The UI can switch per conversation. |
| `LLM_FALLBACK_PROVIDER` | `anthropic` | Used automatically when the chosen provider is unhealthy (empty = no fallback). |
| `AGENT_RUNTIME` | `auto` | `auto` = Agent SDK for Anthropic, Messages loop for Ollama. Force with `agent_sdk` / `messages_loop`. |
| `ANTHROPIC_API_KEY` | *(empty)* | Cloud provider. Never committed. |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-5` | |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | `http://localhost:11434` when running without Docker. |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Any Ollama chat model with tool support (`llama3.1:8b`, `qwen2.5:3b`, …). |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embeddings; retrieval degrades to lexical if unavailable. |
| `DATABASE_URL` | compose-internal | For Supabase/Railway: `postgresql+asyncpg://…` with the `vector` extension enabled. |
| `INGEST_EPISODE_LIMIT` | `0` (all) | Ingest a subset for a quick look. |

### Model toggle and fallback behaviour
- Sidebar **Local | Cloud** switches the provider for the current conversation (`PATCH /api/sessions/{id}`); each message records which provider, model and runtime answered it.
- Before every turn the API health-checks the chosen provider (key present; Ollama reachable *and* model pulled). If it's unhealthy and `LLM_FALLBACK_PROVIDER` is healthy, the turn uses the fallback and the UI marks it (*· fallback* pill + toast with the reason). If neither works you get a `no_provider` error that says exactly what to fix.

### Cloud model (Anthropic)
Put `ANTHROPIC_API_KEY=sk-ant-…` in `.env`, restart `api`. The Cloud toggle turns green. The Agent SDK runtime is used by default for Anthropic (`AGENT_RUNTIME=auto`).

### Local model (Ollama)
Ollama runs on the host for performance. Prefer everything in Docker? `docker compose --profile ollama-in-docker up --build`, then `docker compose exec ollama ollama pull qwen2.5:7b` (and `nomic-embed-text`) and set `OLLAMA_BASE_URL=http://ollama:11434`.

Tip: give the local model more room with `OLLAMA_CONTEXT_LENGTH=8192` in Ollama's environment (default 4096 is enough for chat; essays benefit from 8k).

## Running without Docker

```bash
# Postgres 16 with pgvector on localhost:5432 (e.g. docker run -e POSTGRES_PASSWORD=lenny -p 5432:5432 pgvector/pgvector:pg16)
cd backend && pip install -r requirements.txt
cp ../.env.example .env   # set DATABASE_URL=postgresql+asyncpg://lenny:lenny@localhost:5432/lenny, OLLAMA_BASE_URL=http://localhost:11434
uvicorn app.main:app --reload --port 8000

cd ../frontend && npm ci && npm run dev      # http://localhost:5173 (proxies /api to :8000)
```

## Tests

```bash
cd backend
python -m pytest -q                       # 45 tests: unit + integration (needs Postgres; creates lenny_test)
python -m pytest -q tests/test_units.py tests/test_ingest.py   # unit only, no database
```
Or inside Compose: `make test-integration`.

The integration tests run against a real Postgres and an **in-process fake of the Anthropic Messages API** (`tests/fake_llm.py`) that also fakes Ollama's `/api/tags` and `/api/embed` — so routing, tool calling, streaming, persistence, sanitisation and every failure mode are tested deterministically without models or network. Coverage map is in [PRD → Acceptance criteria](PRD.md#3-acceptance-criteria); the human checklist is [docs/manual-test-plan.md](docs/manual-test-plan.md).

`scripts/smoke.sh [API_BASE]` exercises a running stack end-to-end (readiness → ingest → search → streamed chat → persistence → error envelope).

## Operating it

| Task | Command |
|---|---|
| Readiness (DB, providers with reasons, embedding progress) | `curl localhost:8000/health/ready` / `make status` |
| Logs (JSON, one line per event, `request_id` + `session_id`) | `docker compose logs -f api` |
| Re-ingest after updating transcripts | `curl -X POST localhost:8000/api/admin/ingest` / `make ingest` (incremental, hash-based) |
| Ingest status | `curl localhost:8000/api/admin/ingest/status` |
| Retrieval debugging | `curl "localhost:8000/api/search?q=activation%20metric&k=5"` |
| Wipe everything | `docker compose down -v` |

Log events worth knowing: `routed`, `retrieval`, `turn_complete`, `turn_error`, `provider_fallback`, `artifact_sanitized`, `essay_draft`, `embedding_paused`, `database_unavailable`. See [architecture.md → Observability](architecture.md#9-observability).

## Troubleshooting

| Symptom | Cause → fix |
|---|---|
| Sidebar: **Ollama unreachable at http://host.docker.internal:11434** | Ollama is bound to `127.0.0.1` only. Set `OLLAMA_HOST=0.0.0.0` in Ollama's environment and restart it (Windows: user env var + relaunch from tray). Check from the container: `docker compose exec api curl -s host.docker.internal:11434/api/tags`. |
| **Model 'qwen2.5:7b' not pulled** | `ollama pull qwen2.5:7b` (or change `OLLAMA_MODEL`). |
| **ANTHROPIC_API_KEY not set / looks malformed** | Add the key to `.env`, `docker compose restart api`. |
| Answers are slow (> 60 s) on the local model | CPU-bound. Use `qwen2.5:3b`, lower `RETRIEVAL_TOP_K` to 4, or switch to Cloud. Essays legitimately take 1–2 minutes locally. |
| `no_provider` error when sending | Both providers unhealthy — the message lists both reasons. |
| `database_unavailable` (503) | Postgres not up or wrong `DATABASE_URL`. `docker compose ps db`, `docker compose logs db`. The API keeps running so `/health/ready` can report it. |
| `/health/ready` shows `episodes: 0` for more than 2 minutes | First-boot clone failed (no internet / GitHub blocked). `docker compose logs api | grep ingest`. Re-trigger with `make ingest`. |
| Knowledge base stuck at `0% embedded` | Embeddings need Ollama + `nomic-embed-text`. Not required — retrieval is lexical until then. Status: `curl localhost:8000/api/admin/ingest/status`. |
| Port already in use | Change `WEB_PORT` / `API_PORT` / `POSTGRES_PORT` in `.env`. |
| HTML artifact looks unstyled / missing images | By design: remote CSS/images and scripts are stripped; only inline styles and base64 images render. See [architecture.md → Security](architecture.md#8-security). |
| Tests hang on `TRUNCATE` | Another connection holds the test DB (an old server). The fixture terminates stray backends, but if you run the API against `lenny_test`, stop it first. |

## Project layout

```
backend/
  app/
    api/routes.py          HTTP + SSE endpoints
    agent/                 service (orchestration), router, runtime (Messages loop + Agent SDK), tools, prompts, events
    skills/ship30/         SKILL.md + essay pipeline + checks
    rag/                   ingest (parse/chunk/dedupe), embeddings worker, hybrid retriever
    artifacts/sanitize.py  allow-list sanitizer + CSP
    llm/providers.py       health, client factory, fallback
    db/                    schema.sql, engine, repositories (plain SQL)
    config.py · logging_setup.py · main.py
  tests/                   unit + integration, fake_llm.py, fixtures/
frontend/src/              App.tsx, components/ (Sidebar, ChatView, MessageItem, ArtifactPanel, Markdown), lib/api.ts, styles.css
docker-compose.yml · .env.example · Makefile · scripts/smoke.sh
PRD.md · architecture.md · design.md · docs/manual-test-plan.md · agent-transcripts/
```

## Verified on
- Windows 11, i5-12500H, 16 GB, no dGPU — Docker Desktop (WSL 2) + host Ollama `qwen2.5:7b` — see the demo video.
- Ubuntu 24.04 container (CI-like): full test suite, 45/45 passing in ~15 s.

## Licence & credits
Transcripts © Lenny Rachitsky, archived by [ChatPRD/lennys-podcast-transcripts](https://github.com/ChatPRD/lennys-podcast-transcripts). Ship 30 for 30 principles from the [Ultimate Guide](https://www.ship30for30.com/post/how-to-start-writing-online-the-ship-30-for-30-ultimate-guide). Code MIT.
