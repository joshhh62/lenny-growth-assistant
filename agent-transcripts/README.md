# Agent transcripts

This project was built by directing an AI coding agent (Claude, via the Claude desktop app's agentic mode) and verifying its work. Per the brief, this folder records how that went — including the attempts that failed and how they were corrected. Secrets and personal data have been removed.

| File | What it is |
|---|---|
| [`01-build-log.md`](01-build-log.md) | Chronological log of the build session: decisions, prompts (paraphrased), every failed attempt and its fix |
| [`02-failed-attempts.md`](02-failed-attempts.md) | The same failures indexed by category, with what I learned from each |
| [`03-verification-runs.md`](03-verification-runs.md) | Test runs, screenshots and smoke-test output used to verify the agent's claims |

## How the agent was directed

- **I set the constraints, the agent proposed the design.** Hardware (16 GB, CPU-only), no API key at start, 3-day deadline, "everything in the brief". The agent proposed the dual-runtime architecture and the hybrid-retrieval-without-Ollama design; I approved them because they de-risked the demo.
- **Every claim was verified.** Nothing was accepted as "done" from a description: tests were run (and re-run six times for the flaky one), the UI was driven with a scripted browser and screenshots were inspected, the smoke script was run against a live stack.
- **Failures were kept.** The agent hit real bugs — a concurrency error, a race between an SSE event and a DB commit, a layout bug, a source-data quality problem — and each is documented with the wrong first attempt and the eventual fix.

## What was not automated
Recording the demo video, creating the GitHub repository and running the fresh-machine verification on Windows were done by hand.
