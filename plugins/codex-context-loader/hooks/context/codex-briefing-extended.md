# Codex Integration (OpenAI Codex) — extended

This build makes Codex's review commands **model-invokable** — you (Claude) can run Codex reviews directly, not just the user. Codex is a second AI: a read-only reviewer and a write-capable delegate.

_Codex routes to the GPT-5.6 family; the model and reasoning effort come from the user's `~/.codex/config.toml` unless a flag overrides them._

## Critical rules (always apply)

- **Reviews are READ-ONLY. Never auto-apply fixes.** After presenting `codex:review` / `codex:adversarial-review` findings, STOP — don't edit a single file, even an obvious fix. Ask the user which to fix.
- **Return Codex output verbatim** — findings ordered by severity, exact file:line. No paraphrasing. One `## Claude's assessment` section may follow the block when you have checkable disagreement with evidence attached; Codex and Claude fail differently, and a review nobody contests is worth less.
- **`/codex:task` is write-capable** (Codex edits files) and user-invoked only; `review` / `adversarial-review` are read-only and yours to run.

## Commands you can invoke

- `codex:review` — native read-only review of local git state.
- `codex:adversarial-review` — challenge-the-design review; takes focus text.
- `codex:setup` — check CLI install / auth.

`/codex:task`, `/codex:status`, `/codex:result`, `/codex:cancel` are **user-only**. Suggest a Codex handoff when one would help and let the user type it; point them at status/result/cancel after backgrounding a job.

## Full guide

**Before running a Codex review or delegating work, invoke the `codex:using-codex` skill.** It has every flag, the review output schema, when-to-use guidance for each command, background-vs-wait, and how to prompt Codex. This briefing is just the menu + the safety rules; the skill is the manual.
