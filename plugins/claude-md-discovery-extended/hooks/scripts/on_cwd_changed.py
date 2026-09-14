#!/usr/bin/env python3
"""CwdChanged hook: treat a shell `cd` as a touch of the destination.

A `cd` into a directory holding a `CLAUDE.md` loads nothing — confirmed
in a probe session where the working directory moved into a subdirectory
with its own `CLAUDE.md` and no `InstructionsLoaded` event followed, then
a relative `cat` there produced nothing either. Unlike Bash path
extraction this needs no parsing at all: `new_cwd` is exact.

The event has no channel that reaches Claude, so this only records the
suspicion; `PostToolBatch` or `UserPromptSubmit` emits it.
"""

import sys

from claude_md_lib import State, canon, config_dir, ignored_prefixes, is_ignored
from hook_runner import run


def handle(data: dict) -> None:
    """Record the new working directory as touched.

    Args:
        data (dict): Parsed hook input.
    """
    session_id = data.get("session_id", "")
    new_cwd = data.get("new_cwd") or data.get("cwd") or ""
    if not session_id or not new_cwd:
        sys.exit(0)

    directory = canon(new_cwd)
    config = config_dir()
    if directory == config or directory.startswith(config + "/"):
        sys.exit(0)
    if is_ignored(directory, ignored_prefixes()):
        sys.exit(0)

    state = State(session_id)
    state.suspect(directory, data.get("agent_id") or "")
    state.flush()


if __name__ == "__main__":
    run("on_cwd_changed", handle)
