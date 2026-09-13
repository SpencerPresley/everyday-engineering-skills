# Codex skills available — guardrails

You have `codex:review` / `codex:adversarial-review` (repository-preserving Codex reviews) in your invokable skill list.

Hard rules:
- **A review run is repository-preserving** — Codex judges the work without rewriting it. Acting on the findings follows the task you were dispatched with; if that task was to report, report.
- **Return Codex output verbatim** — don't paraphrase or summarize it. You may add one `## Claude's assessment` section after it for checkable disagreement, with the evidence attached.
- `codex:task` is **user-only** — it carries `disable-model-invocation`, so no subagent can start a write-capable Codex run. If work should be handed to Codex, say so in your report and let the main thread raise it with the user.
- `codex:status` / `codex:result` are yours to call — use them to follow up on a background review instead of asking the main thread to. `codex:cancel`, `codex:setup`, and `codex:transfer` are user-only.

For the full guide (every flag, the review output schema, when to use each), invoke the `codex:using-codex` skill.
