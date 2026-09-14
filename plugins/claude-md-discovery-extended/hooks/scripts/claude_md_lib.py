"""Shared state and discovery logic for claude-md-discovery-extended.

Claude Code reports every instruction file it loads through the
``InstructionsLoaded`` hook, so this plugin never infers what is in
context — it records what Claude Code says. The inference that remains is
confined to one question: which directories did a ``Bash`` command touch?

Two files per session live in the state directory:

``<session>.jsonl``
    Append-only ledger of ``load`` records (an instruction file Claude
    Code loaded) and ``flag`` records (a file this plugin told an agent to
    read). Later lines win for the same key.

``<session>.pending.json``
    Rewritten scratch state: suspected directories awaiting their grace
    window, the trigger index used to attribute loads to agents, and the
    last working directory seen.

Agent scoping
-------------
``InstructionsLoaded`` carries no ``agent_id`` even when a subagent's tool
call caused the load, but it does carry ``trigger_file_path``, which
matches the triggering tool's ``tool_input.file_path`` byte for byte. Tool
events *do* carry ``agent_id``, so the trigger index maps raw trigger
paths to the agent that touched them, and attribution is resolved lazily
at emit time — by then the index is populated, which sidesteps the race
where an ``InstructionsLoaded`` event arrives before the tool batch that
explains it.

Loads with reason ``session_start`` or ``compact`` are treated as visible
to every agent; lazily triggered loads belong to the agent that triggered
them. That is a judgment call: nested loads demonstrably attach to the
triggering agent, while the project and user memory almost certainly reach
subagents too.
"""

import hashlib
import json
import os
import re
import shlex
import time

MEMORY_BASENAMES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md")
CLAUDE_BASENAMES = ("CLAUDE.md", "CLAUDE.local.md")

# Reasons whose loads are visible to every agent in the session.
GLOBAL_LOAD_REASONS = frozenset({"session_start", "compact"})

MAIN_AGENT = ""

# Guards exactly one race: a batch containing both a Read of pkg/mod.py
# (which makes Claude Code load pkg/CLAUDE.md) and a Bash call touching
# pkg/. The Bash raises a suspicion while the Read's InstructionsLoaded is
# still in flight — one was measured landing 24ms *after* the
# PostToolBatch for its own batch. Emitting immediately would tell the
# model to read a file Claude Code is already loading.
#
# Sized against nested_traversal latency only (0.033, 0.035, 0.038, 0.520,
# 1.413s across probe sessions), which is the only reason an event can
# arrive for a file this plugin would flag. A 4.54s path_glob_match was
# also observed but is irrelevant: rules files live in .claude/rules/ and
# `candidate_files` never looks there, so a slow rules load cannot produce
# a false nag.
#
# This only delays — the UserPromptSubmit backstop forces every pending
# suspicion at the turn boundary, so nothing is ever dropped.
GRACE_SECS = 3.0

WALK_MAX_DEPTH = 25

# Hook output, including additionalContext, is capped at 10,000 characters;
# past that Claude Code spills it to a file and substitutes a preview, which
# would defeat the point of inlining. Stay under with room for the wrapper
# prose. A single oversized file is sent to the read-it-yourself path rather
# than being allowed to crowd out every other finding.
INLINE_BUDGET = 8600
INLINE_MAX_FILE = 6000

# A file too large to inline is only *announced*, which delivers nothing
# unless the model acts on it. Announcements are therefore not recorded as
# known, and repeat until the read is actually observed — rate-limited so
# a session working steadily in that directory is not told every batch.
ANNOUNCE_REPEAT_SECS = 300.0

# Re-hashing every loaded instruction file is cheap but not free, so the
# staleness check is throttled rather than run on every batch. It must run
# on the tool path at all, though: doing it only at turn boundaries means
# an edit made while the user watches a long run of tool calls is not
# noticed until they next type, which can be many minutes of work later.
CHANGE_CHECK_SECS = 15.0
BASH_MAX_CANDIDATES = 20

# `2>/dev/null` and friends appear in a large share of commands and always
# resolve to a real directory, so they would otherwise be suspected on
# nearly every call.
BASH_SKIP_PREFIXES = ("/dev/", "/proc/", "/sys/")
TRIG_MAX = 400
FTOUCH_MAX = 200
SUSP_MAX = 200

STATE_MAX_AGE_DAYS = 30
_HASH_READ_CAP = 4 * 1024 * 1024
LOG_MAX_BYTES = 1024 * 1024


def agents_md_enabled() -> bool:
    """Return whether AGENTS.md files participate in discovery.

    Returns:
        bool: `True` unless `CLAUDE_MD_DISCOVERY_AGENTS_MD` is set to a
              falsy value (`0`, `false`, `no`, `off`).
    """
    raw = os.environ.get("CLAUDE_MD_DISCOVERY_AGENTS_MD", "1")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def memory_basenames() -> tuple[str, ...]:
    """Return the instruction-file basenames currently in scope.

    Returns:
        tuple[str, ...]: All memory basenames, or just the CLAUDE.md
                         family when AGENTS.md discovery is disabled.
    """
    if agents_md_enabled():
        return MEMORY_BASENAMES
    return CLAUDE_BASENAMES


def ignored_prefixes() -> tuple[str, ...]:
    """Parse `CLAUDE_MD_DISCOVERY_IGNORE` into normalized path prefixes.

    The variable holds `os.pathsep`-separated absolute (or `~`-prefixed)
    paths. Anything under an ignored prefix is invisible to the plugin.
    `/` works as a kill switch that disables it entirely.

    Returns:
        tuple[str, ...]: Canonicalized path prefixes, possibly empty.
    """
    raw = os.environ.get("CLAUDE_MD_DISCOVERY_IGNORE", "")
    prefixes = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part.startswith("~"):
            part = os.path.expanduser(part)
        if part.startswith("/"):
            prefixes.append(canon(part))
    return tuple(prefixes)


def is_ignored(path: str, prefixes: tuple[str, ...]) -> bool:
    """Return whether a path falls under any ignored prefix.

    Args:
        path (str): Absolute path to test.
        prefixes (tuple[str, ...]): Output of `ignored_prefixes`.
    """
    for prefix in prefixes:
        if prefix == "/" or path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def canon(path: str) -> str:
    """Canonicalize a path for use as a ledger key.

    `InstructionsLoaded` reports `file_path` already symlink-resolved
    (`/private/tmp/...` on macOS) while `trigger_file_path` and tool
    inputs arrive as typed (`/tmp/...`). Without canonicalizing both
    sides every entry would split in two.

    Args:
        path (str): Any absolute or relative path.

    Returns:
        str: The symlink-resolved absolute path, without a trailing slash.
    """
    return os.path.realpath(path).rstrip("/") or "/"


def config_dir() -> str:
    """Return the Claude Code config directory, canonicalized.

    Honors `CLAUDE_CONFIG_DIR`, falling back to `~/.claude`. Everything
    under this tree is Claude Code's own config and installed plugins, so
    discovery never surfaces files from it — touching a plugin path
    happens constantly and would nag about instructions that are either
    already loaded or not a project's at all.

    Returns:
        str: The absolute, symlink-resolved config directory path.
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )
    return canon(raw)


def state_dir() -> str:
    """Return the directory holding per-session state, creating it if needed.

    Lives under the config dir rather than `TMPDIR` so state survives
    reboots and tmp reapers. `CLAUDE_MD_DISCOVERY_STATE_DIR` overrides
    the location (used by tests).

    Returns:
        str: The absolute state directory path.
    """
    path = os.environ.get("CLAUDE_MD_DISCOVERY_STATE_DIR") or os.path.join(
        config_dir(), "plugin-state", "claude-md-discovery-extended"
    )
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _safe_id(session_id: str) -> str:
    """Sanitize a session id for use as a filename.

    Args:
        session_id (str): The Claude Code session identifier.
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "_", session_id) or "unknown"


def ledger_path(session_id: str) -> str:
    """Return the append-only ledger path for a session.

    Args:
        session_id (str): The Claude Code session identifier.
    """
    return os.path.join(state_dir(), f"{_safe_id(session_id)}.jsonl")


def pending_path(session_id: str) -> str:
    """Return the rewritable scratch-state path for a session.

    Args:
        session_id (str): The Claude Code session identifier.
    """
    return os.path.join(state_dir(), f"{_safe_id(session_id)}.pending.json")


def log_path(session_id: str) -> str:
    """Return the diagnostic log path for a session.

    Args:
        session_id (str): The Claude Code session identifier.
    """
    return os.path.join(state_dir(), f"{_safe_id(session_id)}.log")


def log_event(session_id: str, event: str, **fields) -> None:
    """Append a diagnostic event to the session's log file.

    Event-driven, not per-call: steady-state hook invocations log nothing.
    Writing stops past `LOG_MAX_BYTES` so a pathological loop cannot fill
    the disk. Never raises — diagnostics must not break a hook.

    Args:
        session_id (str): The Claude Code session identifier.
        event (str): Short event name (e.g. `flag`, `suppress`, `error`).
        **fields: JSON-serializable event details.
    """
    try:
        path = log_path(session_id)
        try:
            if os.path.getsize(path) > LOG_MAX_BYTES:
                return
        except OSError:
            pass
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
            **fields,
        }
        with open(path, "a") as fh:
            fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        os.chmod(path, 0o600)
    except Exception:
        pass


def hash_file(path: str) -> str | None:
    """Return a content hash for a file, or `None` if unreadable.

    Reads at most `_HASH_READ_CAP` bytes and mixes in the file size so a
    pathological multi-megabyte file still hashes deterministically
    without stalling the hook.

    Args:
        path (str): File to hash.

    Returns:
        str | None: Hex digest, or `None` when the file cannot be read.
    """
    try:
        size = os.path.getsize(path)
        digest = hashlib.sha256(str(size).encode() + b"\x00")
        with open(path, "rb") as fh:
            digest.update(fh.read(_HASH_READ_CAP))
        return digest.hexdigest()
    except OSError:
        return None


def _read_pending(path: str) -> dict:
    """Load the scratch-state file, tolerating absence or corruption.

    Args:
        path (str): Pending-state file path.

    Returns:
        dict: Parsed state, or an empty dict when unreadable.
    """
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_atomic(path: str, text: str) -> None:
    """Replace a file's contents atomically with mode 0600.

    Args:
        path (str): Destination path.
        text (str): Full file contents.
    """
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class State:
    """Per-session view of what Claude Code has loaded and what is pending.

    Attributes:
        loads (dict[str, dict]): Canonical instruction-file path to
            `{"h": hash, "r": load_reason, "g": raw trigger path}`.
        flags (dict[tuple[str, str], str]): `(path, agent)` to the content
            hash this plugin told that agent to read.
        susp (list[list]): Pending `[directory, agent, timestamp]` triples.
        trig (dict[str, str]): Raw tool-input path to the `agent_id` that
            touched it, used to attribute loads.
        cwd (str): Last working directory observed, canonicalized.
    """

    def __init__(self, session_id: str):
        """Load all state for a session.

        Args:
            session_id (str): The Claude Code session identifier.
        """
        self.session_id = session_id
        self.ledger = ledger_path(session_id)
        self.pending = pending_path(session_id)

        self.loads: dict[str, dict] = {}
        self.flags: dict[tuple[str, str], str] = {}
        self.announced: dict[tuple[str, str], float] = {}
        self._appends: list[dict] = []
        self._pending_dirty = False

        self._read_ledger()

        data = _read_pending(self.pending)
        self.susp: list[list] = [
            s for s in data.get("susp", []) if isinstance(s, list) and len(s) == 3
        ]
        self.ftouch: list[list] = [
            f for f in data.get("ftouch", []) if isinstance(f, list) and len(f) == 2
        ]
        self.trig: dict[str, str] = dict(data.get("trig", {}))
        self.cwd: str = data.get("cwd", "") or ""
        self.last_change_check: float = data.get("checked", 0.0) or 0.0

    def _read_ledger(self) -> None:
        """Fold the append-only ledger into `loads` and `flags`."""
        try:
            with open(self.ledger, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    kind, path = obj.get("t"), obj.get("p")
                    if not path:
                        continue
                    if kind == "load":
                        self.loads[path] = {
                            "h": obj.get("h") or "",
                            "r": obj.get("r") or "",
                            "g": obj.get("g") or "",
                        }
                    elif kind == "flag":
                        self.flags[(path, obj.get("a") or MAIN_AGENT)] = (
                            obj.get("h") or ""
                        )
                    elif kind == "ann":
                        self.announced[(path, obj.get("a") or MAIN_AGENT)] = (
                            obj.get("ts") or 0.0
                        )
                    elif kind == "unload":
                        self.loads.pop(path, None)
                    elif kind == "unflag":
                        self.flags.pop((path, obj.get("a") or MAIN_AGENT), None)
        except OSError:
            pass

    def agent_for_load(self, meta: dict) -> str:
        """Return the agent whose context a load entered.

        Args:
            meta (dict): A `loads` value.

        Returns:
            str: The owning `agent_id`, or `MAIN_AGENT` for the main
                 conversation and for session-wide loads.
        """
        if meta.get("r") in GLOBAL_LOAD_REASONS:
            return MAIN_AGENT
        return self.trig.get(meta.get("g") or "", MAIN_AGENT)

    def visible_to(self, meta: dict, agent: str) -> bool:
        """Return whether a load is in the given agent's context.

        Args:
            meta (dict): A `loads` value.
            agent (str): The `agent_id` to test, `MAIN_AGENT` for the main
                conversation.
        """
        if meta.get("r") in GLOBAL_LOAD_REASONS:
            return True
        return self.agent_for_load(meta) == agent

    def is_known(self, path: str, agent: str) -> bool:
        """Return whether an agent has already seen an instruction file.

        Args:
            path (str): Canonical instruction-file path.
            agent (str): The `agent_id` to test.
        """
        meta = self.loads.get(path)
        if meta is not None and self.visible_to(meta, agent):
            return True
        return (path, agent) in self.flags

    def known_hashes(self, agent: str) -> set[str]:
        """Return every content hash already in an agent's context.

        Suppression is by content, not path, so byte-identical copies
        across worktrees, clones, and templated projects never re-flag.
        Unlike the pre-0.5 ledger this draws only on content Claude Code
        reported loading (or that this plugin flagged), never on files
        merely found on disk.

        Args:
            agent (str): The `agent_id` to collect hashes for.
        """
        hashes = {
            meta["h"]
            for meta in self.loads.values()
            if meta["h"] and self.visible_to(meta, agent)
        }
        hashes.update(h for (_, a), h in self.flags.items() if a == agent and h)
        return hashes

    def path_for_hash(self, content_hash: str, agent: str) -> str | None:
        """Return some path holding a hash, for suppression diagnostics.

        Args:
            content_hash (str): Content hash to look up.
            agent (str): The `agent_id` whose view to search.
        """
        for path, meta in self.loads.items():
            if meta["h"] == content_hash and self.visible_to(meta, agent):
                return path
        for (path, a), h in self.flags.items():
            if a == agent and h == content_hash:
                return path
        return None

    def record_load(self, path: str, content_hash: str, reason: str, trigger: str) -> None:
        """Record an instruction file Claude Code reported loading.

        Args:
            path (str): Canonical instruction-file path.
            content_hash (str): Its content hash at load time.
            reason (str): The `load_reason` from the hook input.
            trigger (str): Raw `trigger_file_path`, or `""`.
        """
        meta = {"h": content_hash, "r": reason, "g": trigger}
        if self.loads.get(path) == meta:
            return
        self.loads[path] = meta
        self._appends.append(
            {"t": "load", "p": path, "h": content_hash, "r": reason, "g": trigger}
        )

    def record_flag(self, path: str, content_hash: str, agent: str) -> None:
        """Record that an agent was told to read an instruction file.

        Args:
            path (str): Canonical instruction-file path.
            content_hash (str): Its content hash when flagged.
            agent (str): The `agent_id` that was told.
        """
        if self.flags.get((path, agent)) == content_hash:
            return
        self.flags[(path, agent)] = content_hash
        record = {"t": "flag", "p": path, "h": content_hash}
        if agent:
            record["a"] = agent
        self._appends.append(record)

    def should_announce(self, path: str, agent: str, now: float) -> bool:
        """Return whether an un-inlinable file is due to be announced again.

        Args:
            path (str): Canonical instruction-file path.
            agent (str): The `agent_id` the announcement is for.
            now (float): Epoch seconds.
        """
        last = self.announced.get((path, agent))
        return last is None or now - last >= ANNOUNCE_REPEAT_SECS

    def record_announce(self, path: str, agent: str, now: float) -> None:
        """Record that an agent was told to read a file we could not inline.

        Deliberately not `record_flag`: an announcement delivers nothing
        on its own. If the model reads the file, `mark_direct_access`
        records the flag and the announcement stops; if it does not, the
        content is genuinely absent and the file must surface again.

        Args:
            path (str): Canonical instruction-file path.
            agent (str): The `agent_id` that was told.
            now (float): Epoch seconds.
        """
        self.announced[(path, agent)] = now
        record = {"t": "ann", "p": path, "ts": now}
        if agent:
            record["a"] = agent
        self._appends.append(record)

    def note_trigger(self, raw_path: str, agent: str) -> None:
        """Index a raw tool-input path against the agent that touched it.

        Only subagent touches are indexed: an unindexed trigger resolves
        to the main agent, which is the correct default and keeps the
        index small in normal sessions.

        Args:
            raw_path (str): `tool_input.file_path` exactly as the tool
                received it — `InstructionsLoaded` echoes this spelling in
                `trigger_file_path`, so the join must not normalize.
            agent (str): The `agent_id` from the tool event.
        """
        if not agent or not raw_path or self.trig.get(raw_path) == agent:
            return
        self.trig[raw_path] = agent
        if len(self.trig) > TRIG_MAX:
            for key in list(self.trig)[: len(self.trig) - TRIG_MAX]:
                del self.trig[key]
        self._pending_dirty = True

    def due_for_change_check(self, now: float) -> bool:
        """Return whether the staleness check should run on this batch.

        Args:
            now (float): Epoch seconds.
        """
        return now - self.last_change_check >= CHANGE_CHECK_SECS

    def mark_change_check(self, now: float) -> None:
        """Record that the staleness check just ran.

        Args:
            now (float): Epoch seconds.
        """
        self.last_change_check = now
        self._pending_dirty = True

    def note_file_touch(self, directory: str, now: float) -> None:
        """Record that a *file tool* reached into a directory.

        Reading a file makes Claude Code load the CLAUDE.md of every
        directory from that file up to the project root, and the resulting
        `InstructionsLoaded` events are asynchronous. Remembering where
        file tools have been recently is what lets `is_contended` tell a
        suspicion that must wait from one that can be emitted at once.

        Args:
            directory (str): Canonical directory the file tool reached.
            now (float): Epoch seconds.
        """
        for entry in self.ftouch:
            if entry[0] == directory:
                entry[1] = now
                self._pending_dirty = True
                return
        self.ftouch.append([directory, now])
        if len(self.ftouch) > FTOUCH_MAX:
            self.ftouch = self.ftouch[-FTOUCH_MAX:]
        self._pending_dirty = True

    def is_contended(self, directory: str, now: float) -> bool:
        """Return whether a load for `directory` may still be in flight.

        A file tool at `directory` or anywhere beneath it triggers a
        nested load for `directory`'s CLAUDE.md, so only those touches can
        race. A touch *above* it cannot: the traversal walks upward.

        Args:
            directory (str): Canonical directory under suspicion.
            now (float): Epoch seconds.
        """
        prefix = directory + "/"
        for touched, ts in self.ftouch:
            if now - ts >= GRACE_SECS:
                continue
            if touched == directory or touched.startswith(prefix):
                return True
        return False

    def suspect(
        self,
        directory: str,
        agent: str,
        now: float | None = None,
        contended: bool = False,
    ) -> None:
        """Record a directory a Bash command touched.

        A contended suspicion waits out the grace window; an uncontended
        one is due immediately, so it is emitted by the very
        `PostToolBatch` that observed it. That distinction is what keeps
        the plugin useful: a turn is often a single tool call followed by
        an answer, and a suspicion that always needed a *later* batch
        would sit unemitted until the user happened to type again.

        Args:
            directory (str): Canonical directory path.
            agent (str): The `agent_id` that touched it.
            now (float | None): Epoch seconds, injectable for tests.
            contended (bool): Whether a file tool may still be loading
                this directory's instruction files.
        """
        now = time.time() if now is None else now
        ready_at = now + GRACE_SECS if contended else now
        for entry in self.susp:
            if entry[0] == directory and entry[1] == agent:
                entry[2] = min(entry[2], ready_at)
                self._pending_dirty = True
                return
        self.susp.append([directory, agent, ready_at])
        if len(self.susp) > SUSP_MAX:
            self.susp = self.susp[-SUSP_MAX:]
        self._pending_dirty = True

    def take_due(self, force: bool, now: float | None = None) -> list[tuple[str, str]]:
        """Remove and return suspicions that are ready to emit.

        Args:
            force (bool): Take every suspicion regardless of readiness.
                Used at turn boundaries, where all async loads have landed.
            now (float | None): Epoch seconds, injectable for tests.

        Returns:
            list[tuple[str, str]]: `(directory, agent)` pairs to check.
        """
        now = time.time() if now is None else now
        due, keep = [], []
        for directory, agent, ready_at in self.susp:
            if force or now >= ready_at:
                due.append((directory, agent))
            else:
                keep.append([directory, agent, ready_at])
        if len(keep) != len(self.susp):
            self.susp = keep
            self._pending_dirty = True
        self.ftouch = [f for f in self.ftouch if now - f[1] < GRACE_SECS]
        return due

    def set_cwd(self, cwd: str) -> bool:
        """Update the recorded working directory.

        Args:
            cwd (str): Canonical working directory.

        Returns:
            bool: `True` when this is a change from the previous value.
        """
        if self.cwd == cwd:
            return False
        changed = bool(self.cwd)
        self.cwd = cwd
        self._pending_dirty = True
        return changed

    def drop_lazy_loads(self) -> list[str]:
        """Forget lazily loaded files, e.g. after a worktree switch.

        `ExitWorktree` clears Claude Code's memory-file caches, and
        `EnterWorktree` moves the session into a different checkout, so
        nested loads recorded against the old tree no longer describe what
        Claude Code has loaded. Session-wide loads survive.

        Flags are deliberately kept. A flag means the model read the file
        into its transcript, and a transcript is not cleared by changing
        directories — the content is still there. Dropping flags here
        would discard true knowledge and re-surface a file the model has
        already read. (Flags on the old tree's paths cannot wrongly
        suppress the new tree's copies either: paths are absolute, so a
        new checkout's files are new keys, and identical copies are
        suppressed by content hash on purpose.)

        Returns:
            list[str]: The paths forgotten.
        """
        dropped = [
            path
            for path, meta in self.loads.items()
            if meta.get("r") not in GLOBAL_LOAD_REASONS
        ]
        for path in dropped:
            del self.loads[path]
            self._appends.append({"t": "unload", "p": path})
        return dropped

    def drop_flags(self) -> list[str]:
        """Forget plugin-flagged files, e.g. after compaction.

        A flagged file entered context only through the transcript, which
        compaction drops. Natively loaded files are re-reported by Claude
        Code with `load_reason` `compact`, so they are left alone.

        Returns:
            list[str]: The paths forgotten.
        """
        dropped = sorted({path for path, _ in self.flags})
        for path, agent in list(self.flags):
            del self.flags[(path, agent)]
            record = {"t": "unflag", "p": path}
            if agent:
                record["a"] = agent
            self._appends.append(record)
        # Announcements lived in the transcript too; clearing them lets an
        # un-read large file surface again rather than staying silent.
        for key in list(self.announced):
            self.announced[key] = 0.0
        return dropped

    def flush(self) -> None:
        """Persist buffered ledger appends and rewritten scratch state."""
        if self._appends:
            payload = "".join(
                json.dumps(r, separators=(",", ":")) + "\n" for r in self._appends
            )
            with open(self.ledger, "a") as fh:
                fh.write(payload)
            os.chmod(self.ledger, 0o600)
            self._appends = []
        if self._pending_dirty:
            _write_atomic(
                self.pending,
                json.dumps(
                    {
                        "susp": self.susp,
                        "ftouch": self.ftouch,
                        "trig": self.trig,
                        "cwd": self.cwd,
                        "checked": self.last_change_check,
                    },
                    separators=(",", ":"),
                ),
            )
            self._pending_dirty = False


_REDIRECT_PREFIX = re.compile(r"^[0-9]*[<>]+&?")


def _clean_token(token: str) -> str:
    """Strip shell punctuation that can be glued onto a path token.

    Handles `>out.txt`, `2>>log`, `--file=path`, and trailing separators
    left by `shlex` on constructs like `cmd;`.

    Args:
        token (str): A raw token from `shlex.split`.

    Returns:
        str: The token's path-like remainder, possibly empty.
    """
    token = token.strip().strip("'\"")
    token = _REDIRECT_PREFIX.sub("", token).strip()
    if token.startswith("--") and "=" in token:
        token = token.split("=", 1)[1]
    # Quotes can survive on either side of the redirect strip, and again
    # when shlex bailed out and the caller fell back to whitespace
    # splitting, which does no quote processing at all.
    return token.strip("'\"").rstrip(";&|")


def bash_directories(command: str, cwd: str) -> list[str]:
    """Extract directories a Bash command plausibly touched.

    Best-effort by construction: shell is not parsed, only tokenized.
    Tokens are resolved against `cwd` when relative, which is what makes
    the common `sed -n 1,5p pkg/mod.py` form visible at all, and kept only
    when they (or their parent) exist on disk — which discards flags,
    `sed` scripts, and heredoc body text without special-casing them.

    Failure is asymmetric and deliberately so: a missed path leaves the
    session exactly where it would be without the plugin, while a spurious
    one costs at most one unnecessary instruction file.

    Args:
        command (str): The raw Bash command string.
        cwd (str): Canonical working directory for relative resolution.

    Returns:
        list[str]: Canonical directory paths, de-duplicated, order kept.
    """
    if not command:
        return []
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        # Unbalanced quotes: fall back to whitespace splitting rather than
        # giving up, since a malformed quote elsewhere in a long command
        # should not blind the whole call.
        tokens = command.split()

    dirs: list[str] = []
    seen: set[str] = set()
    for token in tokens[:BASH_MAX_CANDIDATES * 4]:
        token = _clean_token(token)
        if not token or token.startswith("-"):
            continue
        if token.startswith("~"):
            token = os.path.expanduser(token)
        if token.startswith(BASH_SKIP_PREFIXES):
            continue
        if not token.startswith("/"):
            if "/" not in token and "." not in token:
                # Bare words are overwhelmingly subcommands and operands,
                # not paths; requiring a separator or extension keeps
                # `git status` from resolving `status` against cwd.
                continue
            token = os.path.join(cwd, token)

        try:
            if os.path.isdir(token):
                directory = token
            elif os.path.exists(token) or os.path.isdir(os.path.dirname(token)):
                directory = os.path.dirname(token)
            else:
                continue
        except OSError:
            continue

        directory = canon(directory)
        if directory not in seen:
            seen.add(directory)
            dirs.append(directory)
        if len(dirs) >= BASH_MAX_CANDIDATES:
            break
    return dirs


def candidate_files(
    directory: str, cwd: str, ignored: tuple[str, ...]
) -> list[str]:
    """Collect instruction files that a touch at `directory` should load.

    Walks from `directory` upward the way Claude Code's own nested
    traversal does, stopping at any strict ancestor of `cwd` — those were
    loaded at session start and are already reported by
    `InstructionsLoaded`. Per directory, `CLAUDE.md` and `CLAUDE.local.md`
    both count (Claude Code loads both); `AGENTS.md` counts only where no
    `CLAUDE.md` sits beside it, since Claude Code never loads it natively.

    Args:
        directory (str): Canonical directory that was touched.
        cwd (str): Canonical session working directory.
        ignored (tuple[str, ...]): Ignored path prefixes.

    Returns:
        list[str]: Canonical instruction-file paths, nearest first.
    """
    config = config_dir()
    names = memory_basenames()
    found: list[str] = []
    current = directory
    for _ in range(WALK_MAX_DEPTH):
        if current != cwd and cwd.startswith(current + "/"):
            break
        if not (current == config or current.startswith(config + "/")):
            has_claude = False
            for name in names:
                path = os.path.join(current, name)
                if name == "AGENTS.md" and has_claude:
                    continue
                if not os.path.isfile(path) or is_ignored(path, ignored):
                    continue
                if name in CLAUDE_BASENAMES:
                    has_claude = True
                found.append(path)
        if current == "/" or current == cwd:
            break
        parent = os.path.dirname(current) or "/"
        if parent == current:
            break
        current = parent
    return found


def _read_text(path: str) -> str | None:
    """Return a file's text, or `None` when it cannot be decoded or read.

    Args:
        path (str): File to read.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read(INLINE_MAX_FILE + 1)
    except (OSError, UnicodeDecodeError):
        return None


def _render(path: str, text: str) -> str:
    """Render one instruction file the way Claude Code renders its own.

    A natively loaded memory file reaches the model as
    `Contents of <path>:` inside a `<system-reminder>`, and hook
    `additionalContext` is wrapped in a `<system-reminder>` too, so
    matching the inner shape puts this content in the same framing the
    model already associates with project instructions.

    Args:
        path (str): The instruction file's path.
        text (str): Its contents.
    """
    return f"Contents of {path}:\n\n{text.rstrip()}\n"


class Delivery:
    """Plans how each finding reaches the model, and renders the result.

    A finding is *inlined* when its contents fit, and merely *announced*
    when they do not. The distinction matters for bookkeeping, not just
    presentation: inlining delivers the content, so the file can be marked
    known, while an announcement delivers nothing unless the model acts on
    it. Keeping the decision here — rather than splitting the size check
    and the ledger write across two functions — is what stops the two from
    disagreeing and marking an un-delivered file as handled.
    """

    def __init__(self, budget: int = INLINE_BUDGET):
        """Start a delivery plan.

        Args:
            budget (int): Characters available for inlined content.
        """
        self.budget = budget
        self.inline_new: list[str] = []
        self.inline_stale: list[str] = []
        self.announce: list[str] = []

    def add(self, path: str, stale: bool = False) -> bool:
        """Place one finding, inlining it when it fits.

        Args:
            path (str): Canonical instruction-file path.
            stale (bool): Whether this is a changed file rather than a new
                discovery.

        Returns:
            bool: `True` when the contents were inlined, `False` when the
                  file could only be announced.
        """
        text = _read_text(path)
        if text is None or len(text) > INLINE_MAX_FILE:
            self.announce.append(path)
            return False
        block = _render(path, text)
        if len(block) > self.budget:
            self.announce.append(path)
            return False
        self.budget -= len(block)
        (self.inline_stale if stale else self.inline_new).append(block)
        return True

    def empty(self) -> bool:
        """Return whether nothing at all needs to be said."""
        return not (self.inline_new or self.inline_stale or self.announce)

    def message(self) -> str:
        """Render the planned delivery as context for the model.

        Returns:
            str: The message to inject, or `""` when there is nothing.
        """
        if self.empty():
            return ""

        sections: list[str] = []

        if self.inline_new:
            sections.append(
                "These instruction files apply to directories this session "
                "has worked in, but Claude Code did not load them: a nested "
                "CLAUDE.md is only auto-loaded when a file tool reaches its "
                "directory, and Bash operations bypass that. Treat the "
                "contents below as project instructions.\n\n"
                + "\n".join(self.inline_new)
            )

        if self.inline_stale:
            plural = "files have" if len(self.inline_stale) > 1 else "file has"
            sections.append(
                f"The following instruction {plural} changed on disk since "
                "the content was loaded into your context. What follows is "
                "current and supersedes the version you are holding.\n\n"
                + "\n".join(self.inline_stale)
            )

        if self.announce:
            listing = "\n".join(f"  - {path}" for path in self.announce)
            verb = "it" if len(self.announce) == 1 else "each of them"
            sections.append(
                "These instruction files also apply here, but are too large "
                "to include inline, so their contents are NOT in your "
                f"context. IMPORTANT: use the Read tool to read {verb}:\n"
                f"{listing}"
            )

        return (
            "<claude-md-discovery-extended>\n"
            + "\n\n".join(sections)
            + "\n\n"
            "This is a user-installed hook which detects CLAUDE.md files "
            "that do not load, because Claude Code cannot tell which "
            "directories you are reaching into when you use the Bash tool. "
            "Keep working the way you were. It will surface any other "
            "unloaded CLAUDE.md the same way.\n"
            "\n"
            "If any file above references others via @path imports, read "
            "those with the Read tool — imports are only auto-resolved for "
            "natively loaded memory files.\n"
            "\n"
            "Carry on with your current task rather than pausing to report "
            "this. If the user asks what instructions you have loaded, "
            "answer honestly and include these.\n"
            "</claude-md-discovery-extended>"
        )


def build_message(paths: list[str], changed: list[str] | None = None) -> str:
    """Render findings for callers that do not need the delivery plan.

    Args:
        paths (list[str]): Instruction files the agent has not seen.
        changed (list[str] | None): Loaded files whose content changed.

    Returns:
        str: The message to inject as additional context.
    """
    delivery = Delivery()
    for path in paths:
        delivery.add(path)
    for path in changed or []:
        delivery.add(path, stale=True)
    return delivery.message()


def gc_state(now: float | None = None) -> int:
    """Delete state from sessions that never ended cleanly.

    Args:
        now (float | None): Epoch seconds, injectable for tests.

    Returns:
        int: Count of files removed.
    """
    now = time.time() if now is None else now
    cutoff = now - STATE_MAX_AGE_DAYS * 86400
    removed = 0
    try:
        sdir = state_dir()
        for name in os.listdir(sdir):
            path = os.path.join(sdir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.unlink(path)
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


def drop_session(session_id: str) -> None:
    """Delete every state file for a session.

    Args:
        session_id (str): The Claude Code session identifier.
    """
    for path in (
        ledger_path(session_id),
        pending_path(session_id),
        log_path(session_id),
    ):
        try:
            os.unlink(path)
        except OSError:
            pass


def detect_changes(
    state: "State", agent: str, ignored: tuple[str, ...]
) -> list[tuple[str, str]]:
    """Find loaded instruction files whose content changed on disk.

    Claude Code loads a memory file once and never reloads it, so editing
    a CLAUDE.md while a session runs leaves the session working from the
    old rules with no indication. Re-hashing at the turn boundary closes
    that window.

    Reports without committing: the caller records the new hash only once
    the new content has actually been delivered. Marking it current before
    then would bury the change if the file turned out to be too large to
    inline.

    Args:
        state (State): The session state.
        agent (str): The `agent_id` whose view to check.
        ignored (tuple[str, ...]): Ignored path prefixes.

    Returns:
        list[tuple[str, str]]: `(path, new hash)` for each changed file.
    """
    changed: list[tuple[str, str]] = []
    known = state.known_hashes(agent)
    for path, meta in list(state.loads.items()):
        if not state.visible_to(meta, agent) or is_ignored(path, ignored):
            continue
        content_hash = hash_file(path)
        if not content_hash or content_hash == meta["h"]:
            continue
        if content_hash in known:
            # Converged on content already in context; nothing to say, but
            # keep the record current so it is not re-examined.
            state.record_load(path, content_hash, meta["r"], meta["g"])
            continue
        changed.append((path, content_hash))
    return changed


def commit_change(state: "State", path: str, content_hash: str) -> None:
    """Mark a changed file's new content as the one now in context.

    Args:
        state (State): The session state.
        path (str): Canonical instruction-file path.
        content_hash (str): The hash that was just delivered.
    """
    meta = state.loads.get(path)
    if meta:
        state.record_load(path, content_hash, meta["r"], meta["g"])


def seed_root(state: "State", root: str) -> None:
    """Record the session root's own instruction files as loaded.

    Claude Code always loads these at startup. Asserting it costs two
    `stat` calls and closes the only gap where a natively loaded file
    could be flagged: the upward walk stops *above* the session root, so
    the root's own files are the sole natively loaded candidates the
    plugin ever examines.

    Args:
        state (State): The session state.
        root (str): Canonical session working directory.
    """
    ignored = ignored_prefixes()
    relatives = CLAUDE_BASENAMES + tuple(
        os.path.join(".claude", name) for name in CLAUDE_BASENAMES
    )
    for relative in relatives:
        path = os.path.join(root, relative)
        if is_ignored(path, ignored):
            continue
        content_hash = hash_file(path)
        if content_hash:
            state.record_load(path, content_hash, "session_start", "")


def resolve_pending(
    state: State,
    cwd: str,
    ignored: tuple[str, ...],
    delivery: "Delivery",
    force: bool = False,
    now: float | None = None,
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Turn matured suspicions into planned deliveries.

    Records what was actually achieved, not merely what was attempted: a
    file whose contents were inlined is marked known, while one that could
    only be announced is recorded as an announcement and will surface
    again until the model is observed reading it.

    Args:
        state (State): The session state.
        cwd (str): Canonical session working directory.
        ignored (tuple[str, ...]): Ignored path prefixes.
        delivery (Delivery): Plan to add findings to.
        force (bool): Ignore the grace window (turn boundaries).
        now (float | None): Epoch seconds, injectable for tests.

    Returns:
        tuple: `(inlined, announced, suppressed)` — paths whose contents
            were delivered, paths the model was merely told to read, and
            `(candidate, matched)` pairs suppressed by a content hash
            already in that agent's context.
    """
    now = time.time() if now is None else now
    inlined: list[str] = []
    announced: list[str] = []
    suppressed: list[tuple[str, str]] = []
    seen: set[str] = set()

    for directory, agent in state.take_due(force, now):
        if is_ignored(directory, ignored):
            continue
        known = state.known_hashes(agent)
        for path in candidate_files(directory, cwd, ignored):
            if path in seen or state.is_known(path, agent):
                continue
            content_hash = hash_file(path)
            if not content_hash:
                continue
            if content_hash in known:
                suppressed.append(
                    (path, state.path_for_hash(content_hash, agent) or "")
                )
                # Recorded as known so an identical copy is not
                # re-examined on every future touch of the same tree.
                state.record_flag(path, content_hash, agent)
                continue
            seen.add(path)
            if delivery.add(path):
                inlined.append(path)
                known.add(content_hash)
                state.record_flag(path, content_hash, agent)
            elif state.should_announce(path, agent, now):
                announced.append(path)
                state.record_announce(path, agent, now)
            else:
                # Announced recently and still unread; stay quiet until the
                # repeat window reopens.
                delivery.announce.remove(path)

    return inlined, announced, suppressed
