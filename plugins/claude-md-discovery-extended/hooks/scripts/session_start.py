#!/usr/bin/env python3
"""SessionStart hook: anchor the session root and handle context lifecycle.

Almost nothing is seeded here. Pre-0.5 this hook walked every ancestor and
scanned the project tree to guess what Claude Code had loaded;
`InstructionsLoaded` reports it directly, including the `.claude/rules/*.md`
and `CLAUDE.local.md` files the ancestor walk never looked for.

The one exception is the session root's own instruction files. Claude Code
always loads those at startup, and asserting it costs two `stat` calls —
worth it because the ancestor walk stops *above* the root, so the root is
the only natively loaded file a mid-session install could otherwise flag.
`InstructionsLoaded` records the identical fact moments later and the two
fold to one entry.
"""

import sys

from claude_md_lib import (
    State,
    canon,
    drop_session,
    gc_state,
    log_event,
    seed_root,
)
from hook_runner import run



def handle(data: dict) -> None:
    """Prepare session state for one SessionStart event.

    Args:
        data (dict): Parsed hook input.
    """
    session_id = data.get("session_id", "")
    cwd = data.get("cwd", "")
    source = data.get("source", "")
    if not session_id or not cwd:
        sys.exit(0)

    removed = gc_state()
    if removed:
        log_event(session_id, "gc", removed=removed)

    if source == "clear":
        # The context was wiped, so every record of what is loaded is
        # void. Claude Code re-fires session_start loads for the new
        # context, which reseeds the ledger for free.
        drop_session(session_id)
        log_event(session_id, "clear_reset")

    state = State(session_id)

    if source == "compact":
        # Natively loaded files are re-reported with load_reason
        # "compact"; plugin-flagged ones lived only in the transcript and
        # are gone, so they must be eligible to re-flag.
        dropped = state.drop_flags()
        if dropped:
            log_event(session_id, "compact_drop", dropped=dropped)

    root = canon(cwd)
    state.set_cwd(root)
    seed_root(state, root)
    state.flush()


if __name__ == "__main__":
    run("session_start", handle)
