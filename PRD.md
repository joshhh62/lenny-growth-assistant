# PRD — The Lenny Growth Assistant

| | |
|---|---|
| **Engagement** | Internal grounded assistant over Lenny's Podcast transcripts for a product & growth team |
| **Author** | Joshith Sai Panchumarthi (Forward Deployed Engineer take-home) |
| **Status** | v1 shipped — see README for run instructions |
| **Companion docs** | [architecture.md](architecture.md) · [design.md](design.md) · [docs/manual-test-plan.md](docs/manual-test-plan.md) |

---

## 1. Discovery brief

### 1.1 User and problem

**Primary user:** a product manager or growth lead on a small team who already trusts Lenny's Podcast as a source, and who reaches for it at three moments:

1. *"What do the best people say about X?"* — before a decision (pricing change, activation metric, first PM hire). Today: 40 minutes of skipping through YouTube, or trusting memory.
2. *"Turn what we learned into something the team will read."* — a memo, a checklist, a short essay for the internal newsletter. Today: copy-pasting quotes into a doc and rewriting.
3. *"Did they really say that?"* — verifying a claim a colleague attributed to a guest. Today: unverifiable.

**Job to be done:** *get a trustworthy, attributable answer from 270+ hours of expert conversation in under a minute, and turn it into a shareable artifact without leaving the tool.*

**Pain removed:** search time, unverifiable paraphrasing, and the "blank page" step between insight and document.

**Secondary user:** the client engineer who has to run, extend, and trust the system after handoff. Their needs (reproducibility, observability, clear boundaries) shaped as many decisions as the PM's did.

### 1.2 Success metrics

| Metric | Definition | Target for v1 | How it's measured |
|---|---|---|---|
| **Grounding rate** (primary) | % of assistant answers that carry ≥1 citation to a transcript passage | ≥ 90 % | `messages.citations` in Postgres; every answer stores the citations it used |
| **Honest-refusal rate** | % of off-corpus questions where the assistant says the transcripts don't cover it rather than inventing an answer | ≥ 95 % on a 20-question probe set | Manual probe (see test plan §5) |
| **Time-to-first-token** (local) | p50 latency from send to first streamed token on the reference laptop (i5-12500H, 16 GB, CPU-only) with `qwen2.5:7b`, model resident | ≤ 60 s (cloud: ≤ 3 s) | `messages.latency_ms` + client timing; structured `turn_complete` logs |
| **Artifact acceptance** | % of generated artifacts the user opens/copies/downloads rather than immediately re-prompting | ≥ 60 % | Proxy in v1: artifact created without a follow-up "redo" message within 2 turns |
| **Operator metric** | A fresh engineer can go from `git clone` to a grounded answer in ≤ 15 minutes using only the README | Yes/No | Fresh-machine run-through before submission (done — see README → Verified on) |

The primary metric is grounding rate because the product's entire value is *trust*: a fast, fluent answer that can't be traced to an episode is worse than no answer for this user.

### 1.3 Assumptions (because the brief was incomplete)

| # | Assumption | Why it matters | If wrong |
|---|---|---|---|
| A1 | The [ChatPRD transcript repo](https://github.com/ChatPRD/lennys-podcast-transcripts) is the canonical, complete corpus (303 folders). | It's the only source named in the brief. | Ingestion is source-agnostic: any folder of `transcript.md` files with YAML frontmatter works. |
| A2 | **The corpus has data-quality issues we must handle, not hide.** Discovery found 31 folders that duplicate another episode under the wrong slug/guest (e.g. `chip-conley/` contains Maggie Crowley's episode; `julian-shapiro/` contains Julie Zhuo's), and frontmatter `guest` values like "Elena Verna 4.0". | Wrong attribution destroys the trust the product exists to create. | We dedupe by `video_id`, derive the guest from the title suffix when it is a person's name, and strip version suffixes. 268 unique episodes remain. Documented in `architecture.md → Ingestion`. |
| A3 | Users are internal and trusted; there is no auth in v1. A client-generated anonymous user id scopes conversations. | The brief asks for user metadata and sessions, not accounts. | The `users` table and every query already key on `user_id`; adding SSO is a middleware change. |
| A4 | The demo machine is a CPU-only 16 GB Windows laptop. | Determines model choice (`qwen2.5:7b`), context budget (≤ 8k tokens), and why the local path avoids the Agent SDK's prompt overhead. | Config-only change (`OLLAMA_MODEL`, `RETRIEVAL_TOP_K`). |
| A5 | "Ship 30 for 30 style" means the principles in the Ultimate Guide (specific topic, curiosity-gap headline, skimmable wheels-and-spokes structure, 1/3/1 rhythm, rate of revelation, one actionable takeaway) — not a reproduction of anyone's voice. | Defines what the skill encodes and what the programmatic checks validate. | The skill is a Markdown file (`SKILL.md`); editing principles doesn't touch code. |
| A6 | Citations must be verifiable by a human in seconds. | Drives the timestamped chunking so every citation deep-links to the minute in the YouTube video. | — |
| A7 | Cloud provider = Anthropic Claude. OpenAI is not integrated in v1. | One provider done well beats two done loosely; the provider abstraction is one class. | Adding OpenAI = one `Provider` implementation + a config value. |
| A8 | Transcript text may contain prompt-injection-like content (guests read out ads, quote tweets, etc.). | We treat retrieved passages as data: the system prompt says so, and artifacts are sanitized regardless of origin. | — |

### 1.4 Scope

**In scope (v1):**
- Grounded Q&A with inline numbered citations, follow-up questions, session isolation, persistence.
- Two providers (Anthropic cloud, Ollama local) switchable per conversation in the UI, with documented fallback.
- Hybrid retrieval (full-text + pgvector) that degrades gracefully to full-text when Ollama is absent.
- Ship 30 for 30 essay skill with programmatic quality checks and a revision pass.
- Markdown and HTML artifacts rendered beside the chat in a sandboxed viewer, sanitized server-side.
- One-command Docker Compose deployment, structured logs, health/readiness endpoints, automated + manual tests.

**Explicitly out of scope (and why):**
- **Authentication / multi-tenant permissions** — internal tool assumption (A3); would double the surface area without changing the core value.
- **Re-ranking model / query rewriting LLM step** — measurable quality gain is uncertain on a 7B CPU model and would add ~30 % latency per turn; RRF hybrid fusion gives most of the benefit at zero model cost.
- **Automatic transcript refresh (cron)** — the corpus changes weekly at most; a manual `POST /api/admin/ingest` (idempotent, hash-based) is sufficient and safer to operate.
- **Streaming inside artifacts** — artifacts appear when complete; the chat streams. Partial HTML rendering is a poor experience and complicates sanitization.
- **Voice, multi-modal, image artifacts** — not asked for; the viewer's allow-list deliberately excludes remote images.
- **OpenAI provider, Pi agent** — see A7.
- **Full evaluation harness (RAGAS-style)** — replaced with a 20-question manual probe set and unit tests on retrieval; a proper eval set is the first thing I'd build in v1.1.
- **Concurrent generation across sessions** — the server already supports it (a turn per task, a database session per turn), but on the default single-model CPU deployment Ollama serialises inference, so parallel turns queue and both finish later than one. The UI serialises them visibly instead of showing two spinners with one stuck. Worth lifting for a GPU or cloud-only deployment; it is a frontend change of about thirty lines, recorded in [design.md §8](design.md).

### 1.5 Risks and trade-offs

| Risk | Likelihood / impact | Mitigation in v1 | Residual |
|---|---|---|---|
| **Hallucination / unsupported claims** | High / high — the core product risk | Retrieve-then-generate on *every* turn (the model always sees passages); system prompt forbids outside knowledge; only citations the answer references are stored; empty retrieval → explicit "not covered" instruction; essay checker rejects citations to unknown passages | A model can still misattribute within the passages. Mitigated by showing the verbatim passage under each citation so the user can check in one click. |
| **Local-model quality** (7B, CPU) | High / medium | Deterministic router runs skills without relying on the model to pick tools; lean Messages-loop runtime keeps prompts small; conservative context budget; programmatic essay checks + one revision pass | Essays from a 7B model are competent, not brilliant. The UI shows which model answered so expectations are set. |
| **Latency** | Medium / medium | Streaming tokens; status trace ("Searching…", "Drafting…") so waits feel intentional; hybrid retrieval < 20 ms; embeddings computed in the background so the app is usable seconds after ingest | Essay generation on CPU ≈ 60–120 s. Acceptable for a long-form artifact; surfaced as a status line, not a spinner. |
| **Cost** (cloud) | Low / low for internal use | Per-message token usage stored; `max_tokens` and `LLM_MAX_TOOL_ROUNDS` capped; local model is the default | No budget alerts in v1. |
| **Data leakage** | Medium / high if cloud is used carelessly | Local model is default and works with zero keys; provider is visible on every message; no user text leaves the machine unless "Cloud" is chosen; `.env` is git-ignored and `.env.example` has no secrets | Whoever enables the cloud key accepts that prompts go to Anthropic. |
| **Unsafe artifact rendering** (XSS, exfiltration) | Medium / high | Two independent layers: server-side allow-list sanitization (bleach) + CSP, and client-side `<iframe sandbox="">` with an opaque origin. Markdown renders with raw HTML disabled | CSS-only tricks (e.g. layout spoofing) are possible inside the frame; the frame cannot reach the parent page or the network. |
| **Ollama unavailable / model not pulled / DB down / missing key** | High during evaluation / medium | Typed error events with actionable messages ("Run: ollama pull …"), readiness endpoint that names the problem, fallback to the other provider when configured, app starts even if the DB is down so `/health` can explain why | — |
| **Source-data quality** (duplicates, wrong guests) | Certain / high for trust | Dedupe by `video_id`, canonical guest from title, parser version salt so fixes re-ingest automatically | A handful of episodes have no timestamps; their citations link to the video start. |

---

## 2. User flows

### F1 — Ask a grounded question
1. User opens the app → sees six example prompts and the knowledge-base status (episodes indexed, % embedded).
2. Types a question → `POST /api/sessions/{id}/messages` (SSE).
3. UI shows the resolved provider pill (e.g. *Local · qwen2.5:7b*), then a status trace: *Searching transcripts… → Thinking…*
4. Tokens stream in; `[n]` chips appear inline. When the turn completes, the **Sources** block lists only the passages the answer cited, each with guest, episode, timestamp deep-link, and the verbatim passage.
5. Follow-up question → the previous turns are sent as history; short/pronoun follow-ups expand the retrieval query with the prior question.

### F2 — Ask something the transcripts don't cover
Retrieval returns nothing above the similarity threshold → the model is told so → reply: *"The transcripts I have don't cover this…"* plus the nearest supported insight, if any. No citations are fabricated.

### F3 — Ship 30 for 30 essay
1. "Write a Ship 30 essay about pricing" → router classifies **essay**, extracts topic + angle.
2. Trace: *Gathering transcript passages → Drafting the essay (Ship 30 for 30 skill) → [Revising to meet the checklist]* → *Summarising…*
3. Artifact panel opens with the rendered essay (headline, hook, H2 wheels, bullets, bold takeaways, "Do this next", TL;DR, Sources with timestamps). Chat shows a 2–3 sentence summary with citations.

### F4 — Artifact (Markdown or HTML)
"Make an HTML one-pager on activation" → router classifies **artifact_html** → passages retrieved → model produces a complete document → **sanitized** → stored → panel opens (Preview / Source, Copy, Download). The footer states what the sandbox permits.

### F5 — Switch provider / fallback
User toggles **Local ↔ Cloud** in the sidebar (per conversation). If the chosen provider is unhealthy and a fallback is configured, the answer is produced by the fallback and the message pill reads *· fallback* with a toast explaining why. If neither is healthy, a typed `no_provider` error explains exactly what to do.

### F6 — New chat / history
"New chat" creates an isolated session; the sidebar lists recent sessions for this user with provider and recency; sessions can be renamed (double-click) and deleted.

---

## 3. Acceptance criteria

| ID | Criterion | Verified by |
|---|---|---|
| AC1 | A new session has independent context; messages in session A never appear in session B's prompt | `test_sessions_keep_independent_context` |
| AC2 | Every assistant answer stores `citations`, `provider`, `model`, `runtime`, `latency_ms`, token counts | `test_chat_streams_grounded_answer_with_citations` |
| AC3 | Citations deep-link to `youtube.com/watch?v=…&t=<seconds>s` | same + `test_search_endpoint_hybrid_and_deep_links` |
| AC4 | When retrieval is empty the model is told explicitly | `test_empty_retrieval_is_acknowledged` |
| AC5 | The model can call `search_transcripts` mid-turn and the result is fed back | `test_model_can_call_search_tool_mid_turn` |
| AC6 | Follow-ups include prior turns as history | `test_follow_up_carries_history` |
| AC7 | "Write a Ship 30 essay…" produces a Markdown artifact with a Sources block; essay passes structural checks | `test_essay_route_creates_markdown_artifact`, `test_essay_check_*` |
| AC8 | Hostile HTML (script, handlers, iframes, forms, remote images, `javascript:` links, CSS `url()`) is stripped on write; CSP is injected; legitimate CSS survives | `test_html_artifact_is_sanitized_on_write`, `test_sanitizer_*` |
| AC9 | Provider switch is config/UI only; readiness reports each provider with an actionable reason | `test_health_and_readiness`, `test_missing_model_gives_actionable_reason` |
| AC10 | Missing key / Ollama down / model timeout / DB down produce typed, structured errors and never a crash | `test_no_provider_available`, `test_model_timeout_is_reported_and_persisted`, `test_database_down_returns_503` |
| AC11 | Validation errors and 404s follow one error envelope with a request id | `test_validation_errors_are_structured`, `test_not_found_is_structured` |
| AC12 | All 303 source folders parse; 3 timestamp formats supported; duplicates collapse to 268 episodes | `test_ingest.py`, ingest run log |
| AC13 | `docker compose up --build` on a fresh machine yields a working UI with only the README | Manual, fresh-machine run (README → Verified on) |
| AC14 | UI usable at 400 px width; keyboard: Enter/Shift+Enter/Esc; live regions for streaming | Manual test plan §3–4 |

---

## 4. Implementation plan (as executed)

| Phase | Scope | Outcome |
|---|---|---|
| **0. Discovery (½ day)** | Read brief; profile the corpus (formats, sizes, duplicates); read Ship 30 guide; confirm Ollama's Anthropic-compatible API and Agent SDK capabilities | Chose: one client library for both providers; two runtimes; hybrid retrieval that never depends on Ollama |
| **1. Data + retrieval (½ day)** | Parser for 3 transcript formats, timestamp-preserving chunker, dedupe, Postgres schema (FTS + pgvector), background embedding worker, RRF fusion | 268 episodes / 27.5k chunks ingest in ~15 s; lexical search < 10 ms |
| **2. Agent layer (½ day)** | Tool registry, Messages-loop runtime, Agent SDK runtime, deterministic router, Ship 30 skill + checks, artifact sanitizer, orchestration with typed errors | 52 automated tests, fake LLM fixture (incl. the Agent SDK path) |
| **3. UI (½ day)** | React/Vite three-pane app, SSE streaming, citations, artifact viewer with sandbox, provider toggle, responsive + a11y | 12 component tests on the rendering boundary and citation integrity; scripted browser runs at 1440 px and 400 px |
| **4. Ops + docs (½ day)** | Compose, Dockerfiles, `.env.example`, Makefile, smoke test, README/PRD/design/architecture, manual test plan, agent transcripts | Fresh-clone verification, demo video |

**What I'd do next (v1.1):** an offline eval set (50 Q/A pairs with expected episodes) run in CI against both providers; cross-encoder re-ranking behind a flag; scheduled ingest refresh; per-user budgets for the cloud provider. On the frontend, component tests currently cover the rendering boundary and citations — the streaming state machine (session-scoped turns, re-attaching a partial answer) is exercised by hand via the manual test plan and would be the next thing to automate.
