# Operating standard

Five pillars, in priority order: **simplicity, modularity, maintainability, cost-effectiveness, competency.** When they conflict, the earlier one wins. When in doubt, do less.

## Think before coding
State your assumptions; if uncertain, ask. If a request has several readings, surface them rather than silently pick one. If a simpler approach exists, say so. When something is unclear, stop and name it before writing code.

## Simplicity first
Write the minimum that solves the problem, nothing speculative. No features beyond the ask, no abstraction for single-use code, no configurability nobody requested, no error handling for cases that can't happen. If 200 lines could be 50, rewrite it. The test: would a senior engineer call this overcomplicated?

## Surgical changes
Touch only what the task needs. Don't "improve" adjacent code, don't refactor what isn't broken, match the existing style even where you'd do it differently. Remove only the orphans your own change created; leave pre-existing dead code alone and mention it instead. Every changed line should trace to the request.

## Modularity and maintainability
Many small files over few large ones — aim for 200-400 lines, functions under 50. One responsibility per module, low coupling, respect declared module boundaries. Don't repeat logic. Each module earns its own tests, and any new dependency earns a one-line reason.

## Goal-driven execution
Turn a task into a verifiable goal before starting: "fix the bug" becomes "write a failing test, then make it pass." For multi-step work, write a short plan with a check per step and loop until each one verifies.

## Writing that others read
Commit messages, PR descriptions, and review comments are a public record. Write them plain, tight, and competent — like a senior engineer, not a generated bullet list. No filler, no manufactured structure, straight quotes, punctuation used sparingly. Say what changed and why, then stop.

---
*Coding principles above draw on the widely-shared community distillation of Andrej Karpathy's coding-agent guidance.*
