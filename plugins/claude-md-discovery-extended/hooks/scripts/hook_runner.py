"""Boilerplate shared by every hook entry point.

Hooks must never break a session, so unexpected exceptions are swallowed
— but logged with a traceback first, or a crash bug would silently
disable the plugin with nothing to debug from.
"""

import json
import os
import sys
import traceback
from typing import Callable

from claude_md_lib import log_event


def run(script: str, handler: Callable[[dict], None]) -> None:
    """Parse hook input from stdin and invoke a handler under a guard.

    Args:
        script (str): Script name, recorded on error events.
        handler (Callable[[dict], None]): Receives the parsed hook input.
    """
    os.umask(0o077)
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(data, dict):
        sys.exit(0)

    try:
        handler(data)
    except SystemExit:
        raise
    except Exception:
        log_event(
            data.get("session_id", ""),
            "error",
            script=script,
            trace=traceback.format_exc(),
        )
    sys.exit(0)


def emit_context(event_name: str, text: str) -> None:
    """Print an `additionalContext` payload for Claude and exit.

    `PostToolUse`-family exit code 2 is not honored (the tool already
    ran), so structured JSON output on exit 0 is the supported way to put
    text in front of the model without blocking anything.

    Args:
        event_name (str): The `hookEventName` this hook was invoked for.
        text (str): Context to inject before the next model call.
    """
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": text,
                }
            }
        )
    )
    sys.exit(0)
