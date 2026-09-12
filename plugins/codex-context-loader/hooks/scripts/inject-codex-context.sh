#!/bin/bash
set -euo pipefail

# Injects Codex plugin context at SessionStart / SubagentStart, but only when a
# Codex plugin is actually enabled. Which briefing is injected is decided by
# CAPABILITY, not plugin identity: the fork removes `disable-model-invocation`
# from review.md (making the review commands model-invokable), so we grep the
# live review.md instead of trusting the install id — which is unreliable since
# a fork can be installed under either id.
#
# Arg 1: hook mode — "SessionStart" (default) or "SubagentStart".

MODE="${1:-SessionStart}"
SETTINGS="$HOME/.claude/settings.json"
INSTALLED="$HOME/.claude/plugins/installed_plugins.json"
CONTEXT_DIR="${CLAUDE_PLUGIN_ROOT}/hooks/context"

# Need jq and the config files; otherwise stay silent (zero token cost).
command -v jq >/dev/null 2>&1 || exit 0
[ -f "$SETTINGS" ] || exit 0
[ -f "$INSTALLED" ] || exit 0

# Find an enabled Codex plugin id. Prefer the fork's distinct id, fall back to
# the upstream id (which may itself be a fork installed under the old name).
ACTIVE_ID=""
for ID in "codex@SpencerPresley" "codex@openai-codex"; do
  if jq -e --arg id "$ID" '.enabledPlugins[$id] == true' "$SETTINGS" >/dev/null 2>&1; then
    ACTIVE_ID="$ID"
    break
  fi
done
[ -n "$ACTIVE_ID" ] || exit 0

# Locate the installed plugin so we can inspect its commands.
INSTALL_PATH="$(jq -r --arg id "$ACTIVE_ID" '.plugins[$id][0].installPath // empty' "$INSTALLED")"
[ -n "$INSTALL_PATH" ] || exit 0

# Capability detection: review.md WITHOUT `disable-model-invocation` => the
# review commands are model-invokable (fork) => extended briefing.
# review.md may live under commands/ or skills/review/ — a plugin is free to
# move it, and both surfaces behave identically. Probe each, and only treat a
# file we actually found as evidence.
CAPABILITY="base"
for REVIEW_MD in "$INSTALL_PATH/commands/review.md" "$INSTALL_PATH/skills/review/SKILL.md"; do
  if [ -f "$REVIEW_MD" ]; then
    if ! grep -q 'disable-model-invocation' "$REVIEW_MD"; then
      CAPABILITY="extended"
    fi
    break
  fi
done

# Pick the briefing by mode + capability.
if [ "$MODE" = "SubagentStart" ]; then
  # Subagents only need the guardrail, and only where review is invokable.
  [ "$CAPABILITY" = "extended" ] || exit 0
  CONTEXT_FILE="$CONTEXT_DIR/codex-briefing-subagent.md"
  EVENT_NAME="SubagentStart"
else
  if [ "$CAPABILITY" = "extended" ]; then
    CONTEXT_FILE="$CONTEXT_DIR/codex-briefing-extended.md"
  else
    CONTEXT_FILE="$CONTEXT_DIR/codex-briefing.md"
  fi
  EVENT_NAME="SessionStart"
fi

[ -f "$CONTEXT_FILE" ] || exit 0

# Emit as additionalContext (the documented context-injection field for
# SessionStart / SubagentStart).
jq -Rs --arg event "$EVENT_NAME" \
  '{ hookSpecificOutput: { hookEventName: $event, additionalContext: . } }' \
  "$CONTEXT_FILE"
