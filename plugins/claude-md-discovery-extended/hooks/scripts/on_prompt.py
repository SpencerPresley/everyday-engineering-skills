#!/usr/bin/env python3
"""UserPromptSubmit hook: flush every pending suspicion at the turn boundary.

This is also where staleness is checked: Claude Code loads a memory file
once and never reloads it, so a CLAUDE.md edited mid-session leaves the
model working from rules it can no longer see. Re-hashing every loaded
file once per turn closes that window without costing anything on the
tool-call path.

`PostToolBatch` only emits suspicions that have outlived the grace
window, so a directory touched by the last Bash call of a turn would
otherwise sit unreported until the next tool call — which may never come.
A turn boundary is the one moment where every async `InstructionsLoaded`
event has certainly landed, so the grace window is forced open here.

Output semantics: on `UserPromptSubmit`, `additionalContext` is injected
alongside the prompt. Exit 2 would BLOCK the user's prompt and is never
used.
"""

import sys
import time

from claude_md_lib import (
    State,
    Delivery,
    canon,
    commit_change,
    detect_changes,
    ignored_prefixes,
    log_event,
    resolve_pending,
    seed_root,
)
from hook_runner import emit_context, run


def handle(data: dict) -> None:
    """Emit every matured and unmatured suspicion for this session.

    Args:
        data (dict): Parsed hook input.
    """
    session_id = data.get("session_id", "")
    raw_cwd = data.get("cwd", "")
    if not session_id or not raw_cwd:
        sys.exit(0)

    state = State(session_id)
    root = state.cwd or canon(raw_cwd)

    if not state.cwd:
        # The plugin was enabled mid-session, so SessionStart never ran
        # and nothing anchored the root. Seed it here or the root's own
        # CLAUDE.md — which Claude Code certainly loaded — would flag.
        seed_root(state, root)

    ignored = ignored_prefixes()
    delivery = Delivery()

    # Staleness first: a changed file the model is actively working from
    # matters more than a newly discovered one, so it gets the budget.
    changed: list[str] = []
    for path, content_hash in detect_changes(state, "", ignored):
        if delivery.add(path, stale=True):
            commit_change(state, path, content_hash)
            changed.append(path)
        elif state.should_announce(path, "", time.time()):
            state.record_announce(path, "", time.time())
        else:
            delivery.announce.remove(path)

    inlined, announced, suppressed = resolve_pending(
        state, root, ignored, delivery, force=True
    )
    state.flush()

    for candidate, matched in suppressed:
        log_event(session_id, "suppress", path=candidate, matched=matched)

    if delivery.empty():
        sys.exit(0)

    log_event(
        session_id, "flag", trigger="UserPromptSubmit",
        inlined=inlined, announced=announced, changed=changed,
    )
    emit_context("UserPromptSubmit", delivery.message())


if __name__ == "__main__":
    run("on_prompt", handle)
