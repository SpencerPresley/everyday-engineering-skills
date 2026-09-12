# Codex Integration (OpenAI Codex)

The Codex plugin provides OpenAI's Codex as a second AI collaborator you can delegate work to.

_Codex routes to whatever the user's `~/.codex/config.toml` selects — currently the GPT-5.6 family (`gpt-5.6`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`). Some plugin text names an older generation; the guidance still applies._

## Critical rules (read first)

- **Never auto-apply review fixes.** If the user runs `/codex:review` or `/codex:adversarial-review` and you see the output, treat it as read-only: present findings ordered by severity, then STOP and ask which, if any, to fix. Do not edit files off a review — even obvious fixes.
- **Return Codex output verbatim** — no paraphrasing or summarizing of review or rescue output. Keep file paths and line numbers exactly as reported. Verbatim means unfiltered, not mute: after the block you may add one `## Claude's assessment` section when you have something checkable — a finding you can disprove, a `file:line` it misread, a consequence it missed. Attach the evidence; skip the section when you only agree.
- **`codex:rescue` is write-capable** — Codex may edit files in the workspace.
- If Codex isn't set up/authenticated, point the user to `/codex:setup`; don't improvise auth.

## What you can invoke

### `codex:rescue` (Agent delegation)
Hands a task to the `codex:codex-rescue` subagent — debug, fix, implement, investigate, or continue prior Codex work. Use it proactively for a substantial, clearly-bounded handoff; don't grab quick tasks you can finish yourself. The agent is a thin forwarder: it shapes the prompt, hands it to Codex, and returns the result unchanged. Flags: `--background` | `--wait`, `--resume` | `--fresh`, `--model <name|spark>` (`spark` → `gpt-5.3-codex-spark`), `--effort <none|minimal|low|medium|high|xhigh>`. **Write-capable by default.** Leave `--model`/`--effort` unset unless the user asks.

### `codex:setup`
Checks Codex CLI install/auth and optionally toggles the stop-time review gate (runs a Codex review before allowing session end).

## User-only commands (you can't invoke these)

In this build, these are slash commands only the user can run — `codex:review`, `codex:adversarial-review`, `codex:status`, `codex:result`, `codex:cancel`, `codex:transfer`. After launching any background Codex work, point the user to `/codex:status`, `/codex:result`, and `/codex:cancel <id>` to follow up.

## Internal skills (used by the rescue agent — do not invoke directly)

- **codex-cli-runtime** — runtime contract for the rescue agent's single `task` call.
- **codex-result-handling** — how to present Codex output; enforces the no-auto-fix rule.
- **gpt-5-4-prompting** — XML-block prompt engineering for Codex (`<task>`, output contract, verification loop, grounding rules).
