# Build log — session 1 (2026-09-12)

Format: **[me]** = what I asked / decided · **[agent]** = what the agent did · **✗** = a failed attempt · **✓** = the correction. Prompts are paraphrased; tool output is trimmed.

---

## 0. Reading the brief

**[me]** Uploaded the take-home `.docx`; asked the agent to read it thoroughly before we start.

**[agent]** Extracted the document text and the embedded hyperlinks (Ship 30 guide, transcript repo, Pi, Agent SDK, Impeccable). Produced a requirements matrix: hard technical requirements, 8 deliverables, 7 evaluation axes. Flagged: 3-day deadline, Ollama must run on my laptop, transcripts of the agent sessions must be kept from the start, and that "Impeccable" being linked signals a UI-quality bar.

**[me]** Answered its questions: i5-12500H / 16 GB / Iris Xe (no dGPU), no API key yet, build in the cloud and push to GitHub, Docker not installed yet.

**[agent]** Locked decisions: `qwen2.5:7b` + `nomic-embed-text` via Ollama so the demo needs zero keys; Anthropic as the cloud provider; Docker Compose plus a no-Docker path.

## 1. Discovery on the data and the tooling

**[agent]** Cloned the transcript repo: 303 episode folders, 28 MB, YAML frontmatter + timestamped speaker turns. Noted that timestamps make YouTube deep-links possible.

**[agent]** Fetched the Ship 30 guide and extracted the principles (4A angles, WHO/WHAT/WHY headlines with a curiosity gap, wheels & spokes, 1/3/1 rhythm, rate of revelation, Tequila Test).

**[agent]** Checked whether the Claude Agent SDK could drive Ollama. Found Ollama's Anthropic-compatible `/v1/messages` endpoint (tools + streaming supported). Inspected the `claude-agent-sdk` Python package: 96 MB wheel bundling the runtime, `ClaudeAgentOptions` supports `system_prompt`, per-call `env`, in-process MCP tools.

**Decision (the central trade-off):** one tool registry, two runtimes — Agent SDK for cloud, a lean Messages-API loop for local — because the SDK's subprocess prompt overhead is unaffordable on a CPU-bound 7B model.

## 2. Backend

**[agent]** Wrote config (pydantic-settings), structured logging (structlog + request-id middleware), `schema.sql` (users/sessions/messages/artifacts/episodes/chunks/ingest_runs; `tsv` generated column with GIN; nullable `vector(768)` with a partial HNSW index), repositories in plain SQL, ingestion, embeddings worker, hybrid retriever with RRF, providers, prompts, tool registry, both runtimes, router, Ship 30 skill, sanitizer, routes, app factory.

### ✗ Parser missed 30 episodes
First regex only matched `Speaker (00:12:34):`. Test over all 303 files: 30 episodes produced zero turns.
### ✓
Inspected the failures: short episodes use `(12:34):`; two use `Speaker:` with no timestamps; one uses `[00:12:34] Speaker: text` inline. Added the two extra patterns. All 303 parse → 30,670 chunks.

### ✗ Schema failed to apply on first boot
`syntax error at or near "on"` — my naive `split(";")` cut a statement inside a SQL comment that contained a semicolon.
### ✓
Strip `--` comments before splitting. Health endpoint then reported honestly: DB ok, no providers available (expected in the cloud container).

### ✓ Ingest: 303 episodes / 30,670 chunks in 14 seconds.

### ✗ Retrieval returned nothing for "pricing strategy for B2B SaaS"
`websearch_to_tsquery` is AND-semantics; "B2B" and "SaaS" tokenise badly.
### ✓
Strict AND first, then relax to OR over the terms if under-filled.

### ✗ Retrieval showed the same passage twice under two guests ("Madhavan Ramanujam" and "Madhavan Ramanujam 2.0")
Investigated: **33 `video_id`s appear under two folders** in the source repo, and some folders are mis-filed (`chip-conley/` contains Maggie Crowley's episode; `julian-shapiro/` contains Julie Zhuo's; frontmatter `guest` is wrong for those).
### ✓
Dedupe by `video_id` (winner = slug matching the title-derived guest, else longer transcript); derive `guest` from the title suffix. 303 → 272 episodes. Recorded as a discovery finding in the PRD.

### ✗ Server kept old code
Re-ingest didn't apply the dedupe — the running uvicorn had the old module loaded (no `--reload`). Also my `pkill -f "uvicorn app.main"` pattern matched the shell running it, killing my own command (exit 144) several times.
### ✓
Restart properly; use `pkill -f "…mai[n]"` / a detached launcher. Added `PARSER_VERSION` to the content hash so parser changes trigger a re-ingest automatically instead of being silently skipped.

## 3. Sanitizer

### ✗ `<script>alert(1)</script>` left `alert(1)` as visible text
bleach with `strip=True` removes the tag but keeps its text.
### ✓
Remove `script/iframe/object/embed/noscript/template/form` **with their contents** before bleach.

### ✗ Valid base64 images were stripped
`data:` wasn't in the protocol allow-list, and my re-validation ran *after* bleach had already removed the attribute.
### ✓
Allow `data:` at the bleach layer, then post-validate: `img src` must match a strict `data:image/(png|jpeg|gif|webp|svg+xml);base64,…` pattern; `a href` must be `https:/http:/mailto:`. Remote images are dropped on purpose (the CSP would block them anyway, and they're a tracking vector).

## 4. Tests

**[agent]** Built `tests/fake_llm.py`: an in-process fake of the Anthropic Messages API (streaming + tool_use) plus Ollama's `/api/tags` and `/api/embed`, keyed off markers in the prompt. Fixtures: three tiny transcripts covering all three formats plus a deliberate duplicate.

### ✗ `IllegalStateChangeError` from SQLAlchemy in the search endpoint
The retriever ran the lexical query as an `asyncio.Task` concurrently with the vector query **on the same AsyncSession**.
### ✓
Run sequentially (both are single-digit ms). Real bug that would have surfaced under load.

### ✗ "Empty retrieval" test failed — vector search *always* returns neighbours
Cosine nearest-neighbour has no notion of "no result", so off-topic queries got nearest-noise and the "transcripts don't cover this" path never triggered.
### ✓
Added `VECTOR_MIN_SIMILARITY` (0.45 for nomic-embed-text); hits below it are discarded. Product-level fix, not a test tweak.

### ✗ Essay test failed only in the full run
Fixture corpus had a single chunk mentioning "retention"; the skill correctly refused (needs ≥2 passages).
### ✓
Enriched the fixture rather than lowering the product threshold; used 80-token chunks in tests so tiny fixtures yield several chunks.

### ✗ Full suite hung at exit (intermittently, ~1 in 3 runs)
Traced with `faulthandler`: hung in the app's **shutdown**, awaiting the embedding worker's cancellation while it was mid-DB-call.
### ✓
Bounded shutdown: `stop()` waits ≤3 s (`wait_for` + `shield`), `aclose()` and `dispose()` also bounded. Six consecutive green runs, 45 tests in ~14 s. This would also have made `docker compose down` slow — a real operational fix.

## 5. Frontend

**[agent]** React + Vite + TS, plain CSS with tokens, three panes, SSE client, citations, artifact viewer with `sandbox=""`, provider toggle, toasts. Built clean under strict TS on the first try.

**[me]** Insisted on a real visual check, not "it builds". **[agent]** ran the backend against the fake LLM and drove the UI with Playwright at 1440 px and 400 px.

### ✗ Artifact panel: "Artifact not found"
Race: the `artifact` SSE event fired as soon as the row was inserted, but the transaction only committed at the end of the turn; the UI fetched in between.
### ✓
Commit immediately after storing the artifact.

### ✗ Source [1] attributed to "How Lovable hit $200M ARR in one year"
Title had two `|` separators; my "guest = title suffix" rule took a headline as a name.
### ✓
Only accept the suffix when it looks like a person's name (capitalised words, no digits); otherwise use frontmatter, with "N.0" suffixes stripped ("Elena Verna 4.0" → "Elena Verna").

### ✗ Sidebar and artifact panel appeared blank in one screenshot
Not blank — the **whole document had scrolled**: the grid row grew with chat content instead of the message pane scrolling (`min-height: auto` on grid/flex children).
### ✓
`grid-template-rows: minmax(0, 1fr)`, `min-height: 0` on the panes, `body { overflow: hidden }`.

### ✗ Header title stayed "New chat" after the first message
Only the sidebar list was updated. ✓ Update the active session too.

### ✗ Mobile: sidebar stayed open after resizing; suggestions were disabled with no session
✓ Reactive `matchMedia` hook; only one overlay at a time; example prompts create a session and send.

### ✓ Verified the sandbox visually
Hostile HTML (script exfiltrating cookies, `onclick`, `javascript:` link, iframe, remote tracking image, form) rendered as: a styled `<h1>`, a paragraph, and an inert link. Legitimate CSS survived.

## 5b. Local-model hardening and the Agent SDK dry run

**[me]** Worried about what we could not test in the cloud container: the CPU-bound local path and the Agent SDK runtime.

**[agent]** Estimated prompt sizes against Ollama's default 4k context and concluded grounded prompts would be silently truncated. Added: an 8k-context requirement in setup, a readiness warning that reads the loaded model's `context_length` from `/api/ps`, tools-off-by-default for local free-form chat (each tool round re-processes the whole prompt on CPU), an 800-token output cap locally, and a cheaper essay policy locally (8 passages, revise only when badly off).

### ✗ Agent SDK dry run "worked" but never reached our tool
Ran the real SDK subprocess against the fake Messages API. Streaming and results worked, but the fake emitted a bare `search_transcripts` tool name while the SDK registers MCP tools as `mcp__lenny__search_transcripts`, so the CLI reported an unknown tool and the fake happily continued.
### ✓
Made the fake pick the tool name from the request's `tools` list. Re-ran: `tool_call → status → citations → tool_result → token…` in 1.4 s, with our handler executing real retrieval. Turned it into an integration test (`test_agent_sdk_runtime_calls_mcp_tool_and_streams`). 46 tests.

## 5c. Retrieval quality on real questions

**[me]** Asked to see the project running with real outputs.

### ✗ Lexical retrieval surfaced a sponsor read and missed the on-topic episode
"how do I know if I have product market fit" returned passing mentions, not Todd Jackson's PMF-framework episode; "first PM hire" returned an ad read at t=2s.
### ✓
Flag sponsor/housekeeping chunks at ingest (`is_ad`, 1,452 of 27,924) and exclude them from both indexes; add an episode-title boost to the lexical score (`ts_rank_cd(chunk) + 0.5·ts_rank(title)`). The PMF query now lands on the PMF episode. Residual: multi-word conceptual queries ("pricing experiments") stay fuzzy until embeddings are present — expected, and why the hybrid design exists.

## 5d. First run on the laptop (Windows, Docker Desktop, host Ollama)

**[me]** Fresh `docker compose up --build`: images built, transcripts cloned, `ingest_complete` in 15 s. Sidebar green; embeddings started.

### ✗ First question timed out after 120 s
The 7B CPU model was still reading the prompt when the read timeout fired, while the embedding worker competed for the same cores.
### ✓
Embedding worker now pauses while any chat turn is in flight; per-request timeout 300 s; local model gets 4 passages instead of 6. Second run: **158.9 s** end-to-end, streamed, 3 citations, persisted.

### ✗ Two "different" sources were the same passage; the model credited Albert Cheng's words to Elena Verna
(a) Five `guest-20/-30` folders carry a byte-identical transcript under a different video id — a second kind of duplicate my `video_id` dedupe could not see. (b) A small model conflates guests when the question names one.
### ✓
(a) Dedupe by transcript body hash as well (303 → 268 episodes). (b) Guest-scoped retrieval: when the question names a guest, search their episodes first and fall back to the corpus only if that yields < 2 passages; system prompt now forbids cross-guest attribution explicitly.

## 6. Deployment and docs

**[agent]** Dockerfiles, Compose (db/api/web + optional `ollama-in-docker` profile), `.env.example`, Makefile, `scripts/smoke.sh`, auto-ingest on empty DB at startup.

### ✗ Could not build images in the cloud container
Docker Hub is blocked by the sandbox's egress policy. ✓ Removed the optional `# syntax=` directive (needs a registry pull), validated the Compose file structurally, and moved image build verification to the laptop (fresh-clone run). Smoke script passed against the dev stack.

**[agent]** Wrote PRD, architecture.md, design.md, manual test plan, README, this log.

## 7. What I checked by hand
- Read every document for claims I couldn't back with a test or a screenshot and removed them.
- Ran the fresh-clone Docker path on Windows (see `03-verification-runs.md`).
- Recorded the demo video.
