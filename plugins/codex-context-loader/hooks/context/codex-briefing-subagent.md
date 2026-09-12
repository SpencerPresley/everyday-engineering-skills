# Codex skills available — guardrails

You have `codex:review` / `codex:adversarial-review` (read-only Codex reviews) in your invokable skill list.

Hard rules:
- **Reviews are READ-ONLY. Never auto-apply fixes** after review findings — not even obvious ones. Surface them and let the main thread or user decide.
- **Return Codex output verbatim** — don't paraphrase or summarize it. You may add one `## Claude's assessment` section after it for checkable disagreement, with the evidence attached.
- `codex:task` is **user-only** — it carries `disable-model-invocation`, so no subagent can start a write-capable Codex run. If work should be handed to Codex, say so in your report and let the main thread raise it with the user.
- `codex:status` / `codex:result` / `codex:cancel` are user-only — you can't call them.

For the full guide (every flag, the review output schema, when to use each), invoke the `codex:using-codex` skill.
