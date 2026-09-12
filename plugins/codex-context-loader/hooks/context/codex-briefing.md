# Codex Integration (OpenAI Codex)

The Codex plugin provides OpenAI's Codex as a second AI collaborator work can be delegated to.

_Codex routes to the GPT-5.6 family (`gpt-5.6`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`); the installed model and reasoning effort come from the user's `~/.codex/config.toml`, not from this plugin._

## Critical rules (read first)

- **Never auto-apply review fixes.** If the user runs `/codex:review` or `/codex:adversarial-review` and you see the output, treat it as read-only: present findings ordered by severity, then STOP and ask which, if any, to fix. Do not edit files off a review — even obvious fixes.
- **Return Codex output verbatim** — no paraphrasing or summarizing of review or task output. Keep file paths and line numbers exactly as reported. Verbatim means unfiltered, not mute: after the block you may add one `## Claude's assessment` section when you have something checkable — a finding you can disprove, a `file:line` it misread, a consequence it missed. Attach the evidence; skip the section when you only agree.
- **`/codex:task` is write-capable** — Codex may edit files in the workspace. It is user-invoked only; you cannot start one.
- If Codex isn't set up/authenticated, point the user to `/codex:setup`; don't improvise auth.

## What you can invoke

### `codex:setup`
Checks Codex CLI install/auth and optionally toggles the stop-time review gate (runs a Codex review before allowing session end).

## User-only commands (you can't invoke these)

In this build, these are slash commands only the user can run — `codex:review`, `codex:adversarial-review`, `codex:task`, `codex:status`, `codex:result`, `codex:cancel`, `codex:transfer`. After any background Codex work, point the user to `/codex:status`, `/codex:result`, and `/codex:cancel <id>` to follow up.

`/codex:task` hands a task to Codex — debug, fix, implement, investigate, or continue prior Codex work — and is **write-capable by default**. When a substantial, clearly-bounded handoff would help, say so and let the user type it; don't hand over quick work you can finish yourself. Once they run it, the command body runs in this session: you build one `codex-companion.mjs task` call, may attach a `<handoff_context>` block of what this session already established (marked unverified), and return the helper's stdout verbatim. Flags: `--background` | `--wait`, `--resume` | `--fresh`, `--with-session`, `--model <name|spark>`, `--effort <none|minimal|low|medium|high|xhigh>`. Leave `--model`/`--effort` unset unless the user asks — unset means the user's Codex config decides.

## Internal skills

- **codex-result-handling** — the full reference for presenting Codex output. User-invoked only (`/codex:codex-result-handling`); the rules above are the working subset.
- **codex-prompting** — XML-block prompt engineering for Codex (`<task>`, output contract, verification loop, grounding rules). User-invoked only (`/codex:codex-prompting`), so it will not appear in your skill list; `codex:using-codex` carries the condensed version.
