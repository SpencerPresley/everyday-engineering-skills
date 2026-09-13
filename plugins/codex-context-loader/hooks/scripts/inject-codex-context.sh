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
INSTALLED="$HOME/.claude/plugins/installed_plugins.json"
CONTEXT_DIR="${CLAUDE_PLUGIN_ROOT}/hooks/context"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

# Need jq and the install ledger; otherwise stay silent (zero token cost).
command -v jq >/dev/null 2>&1 || exit 0
[ -f "$INSTALLED" ] || exit 0

# Enablement can live in user settings or in either project-scoped settings
# file, and a plugin enabled for one project only appears in the latter. Check
# all of them rather than assuming user scope.
SETTINGS_FILES=(
  "$HOME/.claude/settings.json"
  "$PROJECT_DIR/.claude/settings.json"
  "$PROJECT_DIR/.claude/settings.local.json"
)

is_enabled() {
  local id="$1" file
  for file in "${SETTINGS_FILES[@]}"; do
    [ -f "$file" ] || continue
    if jq -e --arg id "$id" '.enabledPlugins[$id] == true' "$file" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

# Discover Codex plugin ids from the ledger instead of guessing marketplace
# names. `codex@spencer-codex`, `codex@openai-codex`, and any other fork id all
# match; this loader itself (codex-context-loader@...) must not.
ACTIVE_ID=""
while IFS= read -r ID; do
  if is_enabled "$ID"; then
    ACTIVE_ID="$ID"
    break
  fi
done < <(jq -r '.plugins | keys[] | select(startswith("codex@"))' "$INSTALLED")
[ -n "$ACTIVE_ID" ] || exit 0

# Installs are per-project and each entry is pinned to the version that project
# installed, so the ledger holds several entries under one id at different
# versions. Prefer this project's entry, then a user-scoped one, then the
# highest version — never simply the first, which is whichever project happened
# to install first.
INSTALL_PATH="$(
  jq -r --arg id "$ACTIVE_ID" --arg dir "$PROJECT_DIR" '
    .plugins[$id] as $entries
    | ( [ $entries[] | select(.projectPath == $dir) ]
        + [ $entries[] | select(.scope == "user") ]
      ) as $preferred
    | if ($preferred | length) > 0
      then $preferred[0].installPath
      else ( $entries | sort_by(.version | split(".") | map(tonumber? // 0)) | last | .installPath )
      end // empty
  ' "$INSTALLED"
)"
[ -n "$INSTALL_PATH" ] || exit 0
[ -d "$INSTALL_PATH" ] || exit 0

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
