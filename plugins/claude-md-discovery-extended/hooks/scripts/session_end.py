#!/usr/bin/env python3
"""SessionEnd hook: remove state that can never be useful again.

Only `/clear` (reason `"clear"`) wipes the context while ending the
session record, so only then is state deleted. Every other reason keeps
it: a resumed session may reuse its session id, and deleting state here
would make the resume re-flag files already in the restored context.
Abandoned state is garbage-collected by the SessionStart hook instead.
"""

import sys

from claude_md_lib import drop_session, log_event
from hook_runner import run


def handle(data: dict) -> None:
    """Delete session state when the context was wiped.

    Args:
        data (dict): Parsed hook input.
    """
    session_id = data.get("session_id", "")
    if not session_id or data.get("reason") != "clear":
        sys.exit(0)
    log_event(session_id, "clear_delete")
    drop_session(session_id)


if __name__ == "__main__":
    run("session_end", handle)
