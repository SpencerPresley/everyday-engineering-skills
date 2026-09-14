#!/usr/bin/env python3
"""PostToolBatch hook: watch Bash for blind spots, then emit matured findings.

Claude Code loads a nested `CLAUDE.md` when a *file tool* reaches into its
directory. A `Bash` command reaching the same directory loads nothing —
verified directly: `cat sub/x.py` and `grep -rn pat sub/` both produce no
`InstructionsLoaded` event, while `Read` on the same file does. That gap
is what this plugin exists to close.

Runs once per tool batch rather than once per tool, which is both cheaper
and the correct granularity: it is the last point before the next model
call, so injected context lands before Claude reasons again.

Emission is deliberately *not* synchronous. `InstructionsLoaded` is async
and has been observed arriving 24ms after the `PostToolBatch` for the very
batch that triggered it, and up to 4.5s later for a `path_glob_match`. A
suspicion raised by a `Bash` call in the same batch as a `Read` of the
same directory must therefore wait out a grace window, or the plugin would
nag about a file Claude Code is already loading.
"""

import os
import sys

from claude_md_lib import (
    State,
    bash_directories,
    build_message,
    canon,
    config_dir,
    hash_file,
    ignored_prefixes,
    log_event,
    memory_basenames,
    resolve_pending,
    seed_root,
)
from hook_runner import emit_context, run

WORKTREE_TOOLS = {"EnterWorktree", "ExitWorktree"}
WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}


def observe(state: State, tool_calls: list, cwd: str, agent: str) -> None:
    """Index triggers and raise suspicions for one batch of tool calls.

    Args:
        state (State): The session state.
        tool_calls (list): The batch's `tool_calls` array.
        cwd (str): Canonical working directory for relative resolution.
        agent (str): The `agent_id` of the agent that ran the batch.
    """
    config = config_dir()
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        tool_input = call.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            continue

        file_path = tool_input.get("file_path")
        if isinstance(file_path, str):
            state.note_trigger(file_path, agent)
            if (
                call.get("tool_name") in WRITE_TOOLS
                and os.path.basename(file_path) in memory_basenames()
            ):
                # The model just wrote this file, so the new content is
                # already in its context. Refresh the recorded hash or the
                # turn-boundary staleness check would nag about an edit it
                # made itself.
                path = canon(file_path)
                meta = state.loads.get(path)
                content_hash = hash_file(path)
                if meta and content_hash:
                    state.record_load(path, content_hash, meta["r"], meta["g"])

        if call.get("tool_name") != "Bash":
            continue
        command = tool_input.get("command")
        if not isinstance(command, str):
            continue
        for directory in bash_directories(command, cwd):
            if directory == config or directory.startswith(config + "/"):
                continue
            state.suspect(directory, agent)


def handle(data: dict) -> None:
    """Process one resolved tool batch.

    Args:
        data (dict): Parsed hook input.
    """
    session_id = data.get("session_id", "")
    raw_cwd = data.get("cwd", "")
    if not session_id or not raw_cwd:
        sys.exit(0)

    cwd = canon(raw_cwd)
    agent = data.get("agent_id") or ""
    tool_calls = data.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        tool_calls = []

    state = State(session_id)

    # A worktree switch moves the session into a different checkout and
    # clears Claude Code's memory-file caches, so nested loads recorded
    # against the old tree no longer describe what is in context. Detected
    # by tool name rather than by a cwd change, because a plain `cd` also
    # moves cwd and must not invalidate anything.
    if any(
        isinstance(c, dict) and c.get("tool_name") in WORKTREE_TOOLS
        for c in tool_calls
    ):
        if state.cwd != cwd:
            dropped = state.drop_lazy_loads()
            state.set_cwd(cwd)
            log_event(session_id, "worktree_switch", root=cwd, dropped=dropped)

    root = state.cwd or cwd

    if not state.cwd:
        # The plugin was enabled mid-session, so SessionStart never ran
        # and nothing anchored the root. Seed it here or the root's own
        # CLAUDE.md — which Claude Code certainly loaded — would flag.
        seed_root(state, root)
    observe(state, tool_calls, cwd, agent)

    ignored = ignored_prefixes()
    flagged, suppressed = resolve_pending(state, root, ignored)
    state.flush()

    for candidate, matched in suppressed:
        log_event(
            session_id, "suppress", path=candidate, matched=matched, agent=agent
        )

    if not flagged:
        sys.exit(0)

    log_event(session_id, "flag", trigger="PostToolBatch", paths=flagged, agent=agent)
    emit_context("PostToolBatch", build_message(flagged))


if __name__ == "__main__":
    run("on_tool_batch", handle)
