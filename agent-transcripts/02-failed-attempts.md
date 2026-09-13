# Failed attempts, by category

Every ✗ from the build log, grouped, with the lesson. Kept deliberately: the brief asks to see how AI-assisted work was verified and corrected.

## Data quality (3)
| Attempt | Why it failed | Fix | Lesson |
|---|---|---|---|
| One timestamp regex for all transcripts | Corpus has 3 formats | Three patterns, tested across all 303 files | Profile the whole corpus before trusting a sample |
| Trust the folder slug / frontmatter `guest` | 33 duplicate `video_id`s, mis-filed folders, "Name 4.0" guests | Dedupe by `video_id`; guest from title when name-like; strip suffixes | Attribution errors are product failures, not data trivia — surface them in the PRD |
| "Guest = title suffix" everywhere | Some titles have two `\|` with a headline suffix | Name-likeness check with frontmatter fallback | A heuristic needs a negative test set, not just the cases that motivated it |

## Concurrency & lifecycle (4)
| Attempt | Why it failed | Fix | Lesson |
|---|---|---|---|
| Lexical + vector queries in parallel on one `AsyncSession` | Sessions aren't concurrency-safe → `IllegalStateChangeError` | Sequential (ms-level anyway) | Parallelism needs separate sessions; measure before optimising |
| Emit `artifact` event, commit at end of turn | UI fetched before commit → 404 | Commit right after insert | Any event that invites a read must follow the commit |
| Unbounded `await task` on shutdown | Cancel landed mid-DB-call; process hung | `wait_for(shield(task), 3s)` + bounded `aclose/dispose` | Shutdown paths need timeouts as much as request paths |
| Persist the user message immediately, the answer at the end | A client that disconnected mid-turn (tab closed, Stop, network drop) left the session holding a question with no reply, for ever | Catch `CancelledError`, write an `interrupted` assistant message under `asyncio.shield`, then re-raise | If two writes make one logical unit, decide what happens when only the first lands |

## Security (3)
| Attempt | Why it failed | Fix | Lesson |
|---|---|---|---|
| `bleach(strip=True)` alone | Keeps `<script>` **text** | Drop dangerous elements with contents first | Sanitizers strip tags, not intent — test with real payloads |
| Re-validate `data:` images after bleach | bleach had already removed them | Allow at parse layer, then strictly re-validate | Order of layered filters matters |
| Strip the model's `<html>/<head>/<body>` wrapper and reassemble | `<title>` is on the allow-list, so the model's head-`<title>` survived into our `<body>` — invalid HTML | Lift `<title>` into the assembled `<head>`; collapse the blank lines left behind | Rebuilding a document means re-homing its head elements, not just deleting the wrapper |

## Retrieval (2)
| Attempt | Why it failed | Fix | Lesson |
|---|---|---|---|
| AND-only full-text search | Zero hits on multi-term jargon | Relax to OR when under-filled | Precision-first, then recall |
| Vector search with no threshold | Nearest-neighbour always returns something → "not covered" never fired | Similarity floor (0.45) | "No answer" must be a reachable state, and tested |

## UI (5)
| Attempt | Why it failed | Fix | Lesson |
|---|---|---|---|
| Grid/flex panes without `min-height: 0` | Document scrolled; panes looked blank | `minmax(0,1fr)`, `min-height:0`, `overflow:hidden` on body | Screenshot the app while streaming, not just at rest |
| Title updated in list only | Header showed "New chat" | Update active session state too | Derived state in two places drifts |
| `window.innerWidth` at render | Not reactive on resize | `matchMedia` hook | Breakpoints are state |
| Suggestions disabled without a session | Empty state was dead on first load | Create session then send | Test the very first click |
| One global `streaming` flag for the whole app | Switching chats mid-answer pointed the stream's UI updates at a session that was no longer on screen; coming back showed a question with no reply, and the composer stayed stuck on "stop" | Bind the turn to the session that started it: guard every UI write on "is this session on screen", keep the partial answer in a ref, re-attach it on return | Streaming state belongs to the stream's subject, not to the window |

## Generation budgets (2)
| Attempt | Why it failed | Fix | Lesson |
|---|---|---|---|
| One `max_tokens` for essays across providers | 2,200 fit the local model; on a cloud reasoning model the budget is shared with thinking, so the essay stopped mid-word | Provider-aware budgets (8,000 cloud / 2,600 local), same for artifacts | Token ceilings are per-model, not per-feature |
| Checker validated only what was present | Word count, headings and citations all "passed" on a half-written essay | Detect no-terminal-punctuation as truncation; treat as severe and retry | Validate the end state, not just the parts |

## Process (3)
| Attempt | Why it failed | Fix | Lesson |
|---|---|---|---|
| `pkill -f "uvicorn app.main"` | Matched the invoking shell → killed itself (exit 144) | `mai[n]` trick / detached launcher | Know your tools' failure modes |
| Verify via description ("it works") | Not verification | Tests, scripted browser runs, smoke script | The agent's confidence is not evidence |
| Install the latest `claude` CLI in CI so the Agent SDK test runs there | The SDK shells out to that binary. A newer build than the one used in development wired the in-process MCP server differently, so the model was offered no tools: `assert [] == ['search_transcripts']`. 50/51 passed; the build went red on a third-party version bump, not on our code | CI omits the CLI and the test skips itself with a reason; it still runs locally against a known version | Don't gate a build on an unpinned external binary. A test that can only fail because someone else shipped a release is not testing your system |
