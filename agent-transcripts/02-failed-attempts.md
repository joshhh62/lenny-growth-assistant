# Failed attempts, by category

Every ✗ from the build log, grouped, with the lesson. Kept deliberately: the brief asks to see how AI-assisted work was verified and corrected.

## Data quality (3)
| Attempt | Why it failed | Fix | Lesson |
|---|---|---|---|
| One timestamp regex for all transcripts | Corpus has 3 formats | Three patterns, tested across all 303 files | Profile the whole corpus before trusting a sample |
| Trust the folder slug / frontmatter `guest` | 33 duplicate `video_id`s, mis-filed folders, "Name 4.0" guests | Dedupe by `video_id`; guest from title when name-like; strip suffixes | Attribution errors are product failures, not data trivia — surface them in the PRD |
| "Guest = title suffix" everywhere | Some titles have two `\|` with a headline suffix | Name-likeness check with frontmatter fallback | A heuristic needs a negative test set, not just the cases that motivated it |

## Concurrency & lifecycle (3)
| Attempt | Why it failed | Fix | Lesson |
|---|---|---|---|
| Lexical + vector queries in parallel on one `AsyncSession` | Sessions aren't concurrency-safe → `IllegalStateChangeError` | Sequential (ms-level anyway) | Parallelism needs separate sessions; measure before optimising |
| Emit `artifact` event, commit at end of turn | UI fetched before commit → 404 | Commit right after insert | Any event that invites a read must follow the commit |
| Unbounded `await task` on shutdown | Cancel landed mid-DB-call; process hung | `wait_for(shield(task), 3s)` + bounded `aclose/dispose` | Shutdown paths need timeouts as much as request paths |

## Security (2)
| Attempt | Why it failed | Fix | Lesson |
|---|---|---|---|
| `bleach(strip=True)` alone | Keeps `<script>` **text** | Drop dangerous elements with contents first | Sanitizers strip tags, not intent — test with real payloads |
| Re-validate `data:` images after bleach | bleach had already removed them | Allow at parse layer, then strictly re-validate | Order of layered filters matters |

## Retrieval (2)
| Attempt | Why it failed | Fix | Lesson |
|---|---|---|---|
| AND-only full-text search | Zero hits on multi-term jargon | Relax to OR when under-filled | Precision-first, then recall |
| Vector search with no threshold | Nearest-neighbour always returns something → "not covered" never fired | Similarity floor (0.45) | "No answer" must be a reachable state, and tested |

## UI (4)
| Attempt | Why it failed | Fix | Lesson |
|---|---|---|---|
| Grid/flex panes without `min-height: 0` | Document scrolled; panes looked blank | `minmax(0,1fr)`, `min-height:0`, `overflow:hidden` on body | Screenshot the app while streaming, not just at rest |
| Title updated in list only | Header showed "New chat" | Update active session state too | Derived state in two places drifts |
| `window.innerWidth` at render | Not reactive on resize | `matchMedia` hook | Breakpoints are state |
| Suggestions disabled without a session | Empty state was dead on first load | Create session then send | Test the very first click |

## Process (2)
| Attempt | Why it failed | Fix | Lesson |
|---|---|---|---|
| `pkill -f "uvicorn app.main"` | Matched the invoking shell → killed itself (exit 144) | `mai[n]` trick / detached launcher | Know your tools' failure modes |
| Verify via description ("it works") | Not verification | Tests, scripted browser runs, smoke script | The agent's confidence is not evidence |
