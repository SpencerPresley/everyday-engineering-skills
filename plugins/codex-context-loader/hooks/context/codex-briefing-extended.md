# Codex Integration (OpenAI Codex) — extended

This build makes Codex's review commands **model-invokable** — you (Claude) can run Codex reviews directly, not just the user. Codex is a second AI: a repository-preserving reviewer and a write-capable delegate.

_Codex routes to the GPT-5.6 family; the model and reasoning effort come from the user's `~/.codex/config.toml` unless a flag overrides them._

## Critical rules (always apply)

- **A review run is repository-preserving** — `codex:review` / `codex:adversarial-review` judge the work without rewriting it. Acting on the findings afterward follows the task you were given: iterate when you were asked to fix or converge, report and stop when you were only asked to review.
- **Return Codex output verbatim** — findings ordered by severity, exact file:line. No paraphrasing. One `## Claude's assessment` section may follow the block when you have checkable disagreement with evidence attached; Codex and Claude fail differently, and a review nobody contests is worth less.
- **`/codex:task` is write-capable** (Codex edits files) and user-invoked only; `review` / `adversarial-review` leave the repository alone and are yours to run.

## Commands you can invoke

- `codex:review` — native repository-preserving review of local git state.
- `codex:adversarial-review` — challenge-the-design review; takes focus text.

`/codex:task`, `/codex:setup`, `/codex:result`, `/codex:cancel`, `/codex:transfer` are **user-only**; `/codex:status` is yours. Suggest a Codex handoff when one would help and let the user type it.

A backgrounded review notifies you when it finishes and writes the rendered review to its own output file — read that file to collect it. Don't poll, and don't reach for `/codex:status` or `/codex:result` to get output you were already handed. Point the user at `/codex:result <id>` when *they* want to re-read a finished job, since they never saw your notification.

## Where the detail lives

Each skill carries its own flags, targeting rules, and reporting contract — invoke the one you need and read its body; there is no separate manual to load first. `/codex:codex-prompting` and `/codex:codex-result-handling` are user-invoked references if the user wants the long form.
