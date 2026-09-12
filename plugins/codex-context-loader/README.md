# codex-context-loader

Dynamically injects detailed Codex plugin context into Claude Code sessions — but only when the Codex plugin is actually enabled, and tailored to what that build of the plugin can actually do.

![Claude Code Session Start Hook with Plugin Installed](assets/codex-context-loader.png)

## Why this exists

The Codex plugin ships with several skills and an agent, but most of them are internal plumbing with terse, unhelpful descriptions. When installed, they consume context tokens in every session regardless of whether you're using Codex. That's pure bloat when you don't need it.

This plugin solves three problems:

1. **Context bloat**: You might not always want the Codex plugin active, but when you do, you want Claude to actually understand what's available. This hook only fires when the Codex plugin is enabled, so you pay zero tokens when it's disabled.

2. **Lacking descriptions**: The agent-facing Codex skill descriptions are minimal — things like "Internal helper contract for calling the codex-companion runtime from Claude Code" don't tell the model enough to use the tools effectively. The injected briefing covers when to use each command, the available flags, the review output shape, and the critical guardrails (reviews are read-only; never auto-apply fixes).

3. **Capability drift**: A stock Codex plugin keeps `review` / `adversarial-review` user-only (`disable-model-invocation: true`), while a fork can make them model-invokable. The briefing Claude needs differs between those two worlds. This plugin detects which one is installed and injects the matching briefing.

## How it works

A `SessionStart` and a `SubagentStart` hook run the same bash script, which:

1. Finds an enabled Codex plugin id (checks `codex@SpencerPresley`, then `codex@openai-codex`) in `~/.claude/settings.json`. If none is enabled, exits silently with no token cost.
2. Locates that plugin's install path from `~/.claude/plugins/installed_plugins.json` and inspects its `commands/review.md`. **Capability, not identity, decides the briefing** — if `disable-model-invocation` is absent, the review commands are model-invokable (a fork), so the *extended* briefing is used; otherwise the *base* briefing.
3. Injects the right briefing as `hookSpecificOutput.additionalContext`:
   - **Session start** → the full briefing (`codex-briefing-extended.md` for a fork, `codex-briefing.md` for stock).
   - **Subagent start** → a compact guardrail (`codex-briefing-subagent.md`), and only when the review commands are model-invokable (subagents can invoke them, but `SessionStart` never fires for subagents, so they'd otherwise get no guardrail). In the stock case, subagents get nothing.

Identity is deliberately *not* trusted: a fork can be installed under either id, so the live `review.md` frontmatter is the only honest signal of what Claude can actually invoke.

## Editing the briefings

The injected content lives in `hooks/context/`:

- `codex-briefing.md` — stock plugin (setup is model-invokable; review/adversarial/rescue are user-only).
- `codex-briefing-extended.md` — fork (review + adversarial-review are model-invokable by Claude).
- `codex-briefing-subagent.md` — compact safety guardrail injected into subagents.

Edit these directly; no script changes needed. Content is read at hook time and JSON-encoded automatically.
