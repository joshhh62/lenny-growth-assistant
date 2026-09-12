# Manual test plan — UI

Run against `http://localhost:3000` (Compose) or `:5173` (dev). Automated tests cover the API, retrieval, routing, persistence and sanitisation; this plan covers what only a human can judge. ~20 minutes.

Legend: **P** = must pass before release · **S** = should pass.

## 1. First run
| # | Steps | Expected | Pri |
|---|---|---|---|
| 1.1 | Open the app with the backend running and Ollama up | Empty state with headline + 6 prompts; sidebar shows **Local** with a green dot and the model name; knowledge base line shows *272 episodes* | P |
| 1.2 | Stop Ollama (`taskkill /IM ollama.exe /F` or quit from tray); wait 20 s | Local dot turns red with reason "Ollama unreachable…"; if `ANTHROPIC_API_KEY` is set, note says "will fall back to anthropic"; otherwise an amber banner lists the exact commands | P |
| 1.3 | Stop the backend | Red banner "Can't reach the API…"; restart backend → banner clears within 20 s without reload | P |

## 2. Grounded chat
| # | Steps | Expected | Pri |
|---|---|---|---|
| 2.1 | Click prompt "What does Elena Verna say retention has to do with growth?" | Session created; user bubble; provider pill *Local · qwen2.5:7b*; trace *Searching transcripts…* → *Thinking…*; tokens stream; `[n]` chips appear | P |
| 2.2 | After completion, inspect Sources | Only cited numbers listed; each shows guest, episode title, timestamp link; "Show passage" expands verbatim text | P |
| 2.3 | Click a `[2]` chip | Source card [2] gets an accent outline (highlight) | S |
| 2.4 | Click the timestamp link | YouTube opens in a new tab at that second | P |
| 2.5 | Ask a follow-up: "and what about activation?" | Answer stays on Elena's material / references the earlier context; history is preserved | P |
| 2.6 | Ask "What is the capital of Mongolia?" | Answer says the transcripts don't cover it; **no Sources block** | P |
| 2.7 | Press **Stop** mid-stream | Streaming halts; partial text remains; composer re-enabled | S |
| 2.8 | New chat → ask anything → switch back to the first chat in the sidebar | Each conversation shows only its own messages and artifacts | P |

## 3. Skills and artifacts
| # | Steps | Expected | Pri |
|---|---|---|---|
| 3.1 | "Write a Ship 30 for 30 essay on finding product-market fit" | Trace: *Gathering… → Drafting the essay… → (Revising…) → Summarising…*; artifact panel opens with a rendered Markdown essay: H1 headline, hook, 3–6 H2 sections, bullets, bold takeaways, "Do this next", TL;DR, Sources with timestamps; chat shows a 2–3 sentence summary | P |
| 3.2 | Panel → **Source** | Raw Markdown shown; **Copy** copies it; **Download** saves `.md` | S |
| 3.3 | "Create a markdown checklist for running a good user interview" | Markdown artifact opens; title from the `# heading` | P |
| 3.4 | "Make an HTML one-pager summarising the best advice on activation" | HTML artifact renders **styled** inside the panel (fonts, spacing, accent colour) — not raw code; footer says *Sandboxed…* | P |
| 3.5 | In the HTML artifact, try to interact: click any link | Nothing navigates the main app; (links inside the sandbox are inert or open nowhere) | P |
| 3.6 | DevTools → Console while an HTML artifact is open | No script execution; CSP violations (if any) are logged, not executed | P |
| 3.7 | Two artifacts in one session | Tabs appear in the panel; switching tabs switches content; chips under messages reopen the right one | S |
| 3.8 | Press **Esc** | Panel closes; chat expands; chip reopens it | S |

## 4. Layout, keyboard, accessibility
| # | Steps | Expected | Pri |
|---|---|---|---|
| 4.1 | Resize to 1024 px | Three panes still usable; artifact panel narrower | S |
| 4.2 | Resize to 400 px (or DevTools phone) | Sidebar becomes a drawer with scrim; artifact becomes a full-width sheet; no horizontal scroll; composer reachable | P |
| 4.3 | Keyboard only: Tab through sidebar → composer → send | Visible focus ring everywhere; Enter sends; Shift+Enter adds a newline | P |
| 4.4 | Screen reader (NVDA/VoiceOver) on a streaming answer | Live region announces new content politely; error toasts announce assertively; icon buttons have names | S |
| 4.5 | OS dark mode | Palette switches; contrast remains readable; artifacts (white iframe) remain legible | S |
| 4.6 | Double-click the conversation title → type → Enter | Renamed in header and sidebar | S |
| 4.7 | Delete a session | Confirmation dialog; session and its artifacts disappear | P |

## 5. Grounding probe set (honest-refusal metric)
Ask each; record whether the assistant (a) answers with citations, (b) says the transcripts don't cover it, or (c) invents. Target: 0 × (c).

1. What does Elena Verna say about product-led sales? *(expect a)*
2. How does Shreyas Doshi describe product sense? *(a)*
3. What did Brian Chesky say about founder mode? *(a)*
4. What's the best pricing model for a B2B SaaS? *(a — Madhavan Ramanujam material)*
5. When should a startup hire its first growth person? *(a)*
6. What is the Tequila Test? *(b — Ship 30 concept, not in transcripts)*
7. What did the guest say about Kubernetes autoscaling? *(b)*
8. Who won the 2022 World Cup? *(b)*
9. Summarise Lenny's episode with Barack Obama. *(b — no such episode; must not invent)*
10. What does Marty Cagan think of "product management theater"? *(a)*
11–20. Pick ten questions from your team's real backlog.

## 6. Operations
| # | Steps | Expected | Pri |
|---|---|---|---|
| 6.1 | `docker compose down -v && docker compose up --build` on a machine that has never run it | Within ~3 min: `web` serves the UI, `api` clones transcripts and ingests (log `ingest_complete`), `/health/ready` reports `ready` | P |
| 6.2 | `scripts/smoke.sh` | All checks ✓ | P |
| 6.3 | `make status` after 10 minutes with Ollama up | `embeddings.status: running` or `complete`, `embedded` increasing | S |
| 6.4 | Tail `docker compose logs -f api` during a chat | JSON lines with `request_id`, `session_id`, `routed`, `retrieval`, `turn_complete` | S |
