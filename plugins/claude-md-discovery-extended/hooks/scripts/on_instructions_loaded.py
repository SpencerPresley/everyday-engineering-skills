#!/usr/bin/env python3
"""InstructionsLoaded hook: record what Claude Code actually loaded.

This is the plugin's only source of truth for "in context". Every other
hook either feeds it (the trigger index) or consumes it (discovery).
The event is observational — Claude Code discards its output — so this
script never prints anything.
"""

import sys

from claude_md_lib import State, canon, hash_file, ignored_prefixes, is_ignored
from hook_runner import run


def handle(data: dict) -> None:
    """Record one loaded instruction file.

    Args:
        data (dict): Parsed hook input.
    """
    session_id = data.get("session_id", "")
    file_path = data.get("file_path", "")
    if not session_id or not file_path:
        sys.exit(0)

    path = canon(file_path)
    if is_ignored(path, ignored_prefixes()):
        sys.exit(0)

    content_hash = hash_file(path)
    if not content_hash:
        sys.exit(0)

    state = State(session_id)
    state.record_load(
        path,
        content_hash,
        data.get("load_reason") or "",
        # Kept in the raw spelling Claude Code reported: it is joined
        # against tool inputs byte for byte, so canonicalizing it here
        # would break attribution on symlinked paths like macOS /tmp.
        data.get("trigger_file_path") or "",
    )
    state.flush()


if __name__ == "__main__":
    run("on_instructions_loaded", handle)
