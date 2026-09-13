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

`/codex:task`, `/codex:setup`, `/codex:status`, `/codex:result`, `/codex:cancel` are **user-only**. Suggest a Codex handoff when one would help and let the user type it; point them at status/result/cancel after backgrounding a job.

## Full guide

**Before running a Codex review or delegating work, invoke the `codex:using-codex` skill.** It has every flag, the review output schema, when-to-use guidance for each command, background-vs-wait, and how to prompt Codex. This briefing is just the menu + the safety rules; the skill is the manual.
