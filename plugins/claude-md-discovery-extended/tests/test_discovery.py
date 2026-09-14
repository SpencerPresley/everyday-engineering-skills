"""Tests for claude-md-discovery-extended.

The plugin's contract in one sentence: a CLAUDE.md that Claude Code
reports loading must never be flagged, and a CLAUDE.md whose directory
was reached only through Bash must be. Most tests below are one concrete
instance of that sentence.

Run with: uv run pytest tests/ -v
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "hooks" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import claude_md_lib as lib  # noqa: E402

ON_IL = str(SCRIPTS / "on_instructions_loaded.py")
ON_BATCH = str(SCRIPTS / "on_tool_batch.py")
ON_CWD = str(SCRIPTS / "on_cwd_changed.py")
ON_PROMPT = str(SCRIPTS / "on_prompt.py")
SESSION_START = str(SCRIPTS / "session_start.py")
SESSION_END = str(SCRIPTS / "session_end.py")

STRIPPED_VARS = (
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_MD_DISCOVERY_STATE_DIR",
    "CLAUDE_MD_DISCOVERY_AGENTS_MD",
    "CLAUDE_MD_DISCOVERY_IGNORE",
)

_sid_counter = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_script(script: str, payload: dict, env: dict) -> tuple[int, str]:
    """Run a hook script with `payload` as JSON on stdin.

    Returns:
        tuple[int, str]: Exit code and stdout.
    """
    proc = subprocess.run(
        [sys.executable, script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout


FRESH_LEAD = "These instruction files apply to directories"
STALE_LEAD = "changed on disk since the content was loaded"
OVERFLOW_LEAD = "too large to include inline"


def parse_message(stdout: str) -> dict[str, list[str]]:
    """Split a hook's emitted context into its three sections.

    Returns:
        dict[str, list[str]]: Paths under keys `new`, `changed`, and
            `overflow`. All empty when the hook stayed silent.
    """
    out: dict[str, list[str]] = {"new": [], "changed": [], "overflow": []}
    if not stdout.strip():
        return out
    text = json.loads(stdout)["hookSpecificOutput"]["additionalContext"]

    section = None
    for line in text.splitlines():
        if FRESH_LEAD in line:
            section = "new"
        elif STALE_LEAD in line:
            section = "changed"
        elif OVERFLOW_LEAD in line:
            section = "overflow"
        elif line.startswith("Contents of ") and line.endswith(":") and section:
            out[section].append(line[len("Contents of "):-1])
        elif line.startswith("  - ") and section == "overflow":
            out["overflow"].append(line.strip()[2:].strip())
    return out


def flagged_paths(stdout: str) -> list[str]:
    """Return every newly discovered path, inlined or too large to inline.

    Returns:
        list[str]: Flagged paths, empty when the hook stayed silent.
    """
    parsed = parse_message(stdout)
    return parsed["new"] + parsed["overflow"]


def inlined_content(stdout: str) -> str:
    """Return the emitted context verbatim, for content assertions."""
    if not stdout.strip():
        return ""
    return json.loads(stdout)["hookSpecificOutput"]["additionalContext"]


def collect(session, command: str, agent: str = "", cwd: str = None) -> str:
    """Run one Bash batch and return whichever hook emitted.

    An uncontended suspicion is emitted by the very batch that observed
    it; a contended one waits and is forced at the turn boundary. Tests
    should not care which fired unless that is what they are testing.

    Returns:
        str: Raw hook stdout, empty when nothing was emitted.
    """
    code, out = run_script(
        ON_BATCH,
        batch(session.sid, cwd or session.cwd, [bash_call(command)], agent),
        session.env,
    )
    assert code == 0
    if out.strip():
        return out
    _, out = run_script(
        ON_PROMPT, {"session_id": session.sid, "cwd": session.cwd}, session.env
    )
    return out


def next_sid(label: str = "s") -> str:
    """Return a session id unique within this test run."""
    global _sid_counter
    _sid_counter += 1
    return f"pytest-{label}-{_sid_counter}-{os.getpid()}"


def batch(sid: str, cwd: str, calls: list[dict], agent: str = "") -> dict:
    """Build a PostToolBatch payload."""
    payload = {
        "session_id": sid,
        "cwd": cwd,
        "hook_event_name": "PostToolBatch",
        "tool_calls": calls,
    }
    if agent:
        payload["agent_id"] = agent
    return payload


def bash_call(command: str) -> dict:
    """Build one Bash entry for a tool batch."""
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def read_call(file_path: str) -> dict:
    """Build one Read entry for a tool batch."""
    return {"tool_name": "Read", "tool_input": {"file_path": file_path}}


def il_payload(sid: str, cwd: str, file_path: str, reason: str, trigger: str = "") -> dict:
    """Build an InstructionsLoaded payload."""
    payload = {
        "session_id": sid,
        "cwd": cwd,
        "hook_event_name": "InstructionsLoaded",
        "file_path": file_path,
        "load_reason": reason,
    }
    if trigger:
        payload["trigger_file_path"] = trigger
    return payload


def state_file(env: dict, sid: str, suffix: str) -> Path:
    """Return a session state file path."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", sid)
    return Path(env["CLAUDE_MD_DISCOVERY_STATE_DIR"]) / f"{safe}{suffix}"


def read_log(env: dict, sid: str) -> list[dict]:
    """Parse a session's diagnostic log."""
    path = state_file(env, sid, ".log")
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def backdate_announcements(env: dict, sid: str) -> None:
    """Age every announcement past its repeat window."""
    path = state_file(env, sid, ".jsonl")
    lines = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    stale = [
        json.dumps({**o, "ts": 0.0}) for o in lines if o.get("t") == "ann"
    ]
    if stale:
        with open(path, "a") as fh:
            fh.write("\n".join(stale) + "\n")


def age_suspicions(env: dict, sid: str, seconds: float) -> None:
    """Backdate every pending suspicion so its grace window has elapsed."""
    path = state_file(env, sid, ".pending.json")
    data = json.loads(path.read_text())
    for entry in data["susp"]:
        entry[2] -= seconds
    path.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def hook_env(tmp_path):
    """Isolated environment with state and config dirs pinned under tmp."""
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_VARS}
    env["CLAUDE_MD_DISCOVERY_STATE_DIR"] = str(tmp_path / "plugin-state")
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude-config")
    (tmp_path / "claude-config").mkdir(exist_ok=True)
    return env


@pytest.fixture()
def layout(tmp_path):
    """Build a project tree with nested and out-of-tree instruction files.

    workspace/project/            cwd, CLAUDE.md
    workspace/project/pkg/        CLAUDE.md
    workspace/project/pkg/deep/   (no instruction file)
    workspace/project/plain/      (no instruction file)
    workspace/sibling/            CLAUDE.md
    """
    ws = tmp_path / "workspace"
    project = ws / "project"
    for rel in ("pkg/deep", "plain"):
        (project / rel).mkdir(parents=True)
    (ws / "sibling").mkdir(parents=True)

    (project / "CLAUDE.md").write_text("# root rules\n")
    (project / "pkg" / "CLAUDE.md").write_text("# pkg rules\n")
    (ws / "sibling" / "CLAUDE.md").write_text("# sibling rules\n")

    (project / "pkg" / "mod.py").write_text("x = 1\n")
    (project / "pkg" / "deep" / "deep.py").write_text("y = 2\n")
    (project / "plain" / "plain.py").write_text("z = 3\n")
    (ws / "sibling" / "sib.py").write_text("w = 4\n")

    return {
        "ws": str(ws),
        "project": os.path.realpath(project),
        "pkg": os.path.realpath(project / "pkg"),
        "deep": os.path.realpath(project / "pkg" / "deep"),
        "plain": os.path.realpath(project / "plain"),
        "sibling": os.path.realpath(ws / "sibling"),
        "root_md": os.path.realpath(project / "CLAUDE.md"),
        "pkg_md": os.path.realpath(project / "pkg" / "CLAUDE.md"),
        "sibling_md": os.path.realpath(ws / "sibling" / "CLAUDE.md"),
    }


@pytest.fixture()
def session(hook_env, layout):
    """Start a session and report the project root CLAUDE.md as loaded.

    Returns a helper object exposing the verbs a session performs.
    """
    sid = next_sid()
    cwd = layout["project"]

    class Session:
        """Driver for one simulated Claude Code session."""

        def __init__(self):
            self.sid = sid
            self.cwd = cwd
            self.env = hook_env

        def start(self, source="startup"):
            """Fire SessionStart."""
            return run_script(
                SESSION_START,
                {"session_id": sid, "cwd": self.cwd, "source": source},
                hook_env,
            )

        def loaded(self, path, reason="session_start", trigger=""):
            """Fire InstructionsLoaded for `path`."""
            return run_script(
                ON_IL, il_payload(sid, self.cwd, path, reason, trigger), hook_env
            )

        def tools(self, calls, agent="", cwd=None):
            """Fire PostToolBatch, returning flagged paths."""
            code, out = run_script(
                ON_BATCH, batch(sid, cwd or self.cwd, calls, agent), hook_env
            )
            assert code == 0
            return flagged_paths(out)

        def bash(self, command, agent="", cwd=None):
            """Fire PostToolBatch with a single Bash call."""
            return self.tools([bash_call(command)], agent, cwd)

        def discover(self, command, agent="", cwd=None):
            """Run a Bash command and return whatever was surfaced."""
            return flagged_paths(collect(self, command, agent, cwd))

        def turn(self):
            """Fire UserPromptSubmit, returning flagged paths."""
            code, out = run_script(
                ON_PROMPT, {"session_id": sid, "cwd": self.cwd}, hook_env
            )
            assert code == 0
            return flagged_paths(out)

        def cd(self, new_cwd, agent=""):
            """Fire CwdChanged."""
            payload = {
                "session_id": sid,
                "cwd": new_cwd,
                "old_cwd": self.cwd,
                "new_cwd": new_cwd,
            }
            if agent:
                payload["agent_id"] = agent
            return run_script(ON_CWD, payload, hook_env)

        def log(self):
            """Return this session's diagnostic events."""
            return read_log(hook_env, sid)

    sess = Session()
    sess.start()
    sess.loaded(layout["root_md"])
    return sess


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

class TestBasicValidation:
    """Hooks must exit 0 and stay silent on unusable input."""

    @pytest.mark.parametrize("script", [ON_IL, ON_BATCH, ON_CWD, ON_PROMPT,
                                        SESSION_START, SESSION_END])
    def test_empty_object(self, script, hook_env):
        assert run_script(script, {}, hook_env) == (0, "")

    @pytest.mark.parametrize("script", [ON_IL, ON_BATCH, ON_CWD, ON_PROMPT])
    def test_missing_session_id(self, script, hook_env, layout):
        code, out = run_script(script, {"cwd": layout["project"]}, hook_env)
        assert (code, out) == (0, "")

    def test_malformed_stdin_exits_zero(self, hook_env):
        proc = subprocess.run(
            [sys.executable, ON_BATCH], input="not json",
            capture_output=True, text=True, env=hook_env,
        )
        assert proc.returncode == 0

    def test_batch_without_tool_calls(self, session):
        assert session.tools([]) == []

    def test_batch_with_junk_tool_calls(self, session):
        code, out = run_script(
            ON_BATCH,
            {"session_id": session.sid, "cwd": session.cwd,
             "tool_calls": ["nonsense", {"tool_input": "notadict"}, {}]},
            session.env,
        )
        assert (code, out) == (0, "")


# ---------------------------------------------------------------------------
# Bash path extraction (pure functions)
# ---------------------------------------------------------------------------

class TestBashExtraction:
    """`bash_directories` is the plugin's only inference layer."""

    def test_absolute_file(self, layout):
        assert lib.bash_directories(f"cat {layout['pkg']}/mod.py", layout["project"]) == [
            layout["pkg"]
        ]

    def test_relative_file_resolves_against_cwd(self, layout):
        # The pre-0.5 extractor accepted only tokens starting with / or ~,
        # so every relative path — most of what gets typed — was invisible.
        assert lib.bash_directories("sed -n 1,5p pkg/mod.py", layout["project"]) == [
            layout["pkg"]
        ]

    def test_directory_token(self, layout):
        assert lib.bash_directories("grep -rn x pkg/", layout["project"]) == [
            layout["pkg"]
        ]

    def test_pipeline_keeps_both_sides(self, layout):
        found = lib.bash_directories(
            f"cat {layout['pkg']}/mod.py | head -3", layout["project"]
        )
        assert layout["pkg"] in found

    def test_redirect_target(self, layout):
        found = lib.bash_directories("echo hi > pkg/out.txt", layout["project"])
        assert layout["pkg"] in found

    def test_flag_value_after_equals(self, layout):
        found = lib.bash_directories(
            f"prog --file={layout['pkg']}/mod.py", layout["project"]
        )
        assert layout["pkg"] in found

    def test_flags_are_not_paths(self, layout):
        assert lib.bash_directories("ls -la -R", layout["project"]) == []

    def test_bare_words_are_not_paths(self, layout):
        assert lib.bash_directories("git status", layout["project"]) == []

    def test_sed_script_is_not_a_path(self, layout):
        found = lib.bash_directories("sed -n 1,5p pkg/mod.py", layout["project"])
        assert found == [layout["pkg"]]

    def test_nonexistent_path_ignored(self, layout):
        assert lib.bash_directories("cat /no/such/place/x.py", layout["project"]) == []

    def test_new_file_in_existing_dir_counts(self, layout):
        found = lib.bash_directories(f"touch {layout['pkg']}/brand_new.py", layout["project"])
        assert found == [layout["pkg"]]

    def test_unbalanced_quotes_fall_back(self, layout):
        # shlex raises here; giving up entirely would blind the whole call.
        found = lib.bash_directories(f"cat \"{layout['pkg']}/mod.py", layout["project"])
        assert layout["pkg"] in found

    def test_empty_command(self, layout):
        assert lib.bash_directories("", layout["project"]) == []

    def test_deduplicates_and_preserves_order(self, layout):
        found = lib.bash_directories(
            f"diff {layout['pkg']}/mod.py {layout['pkg']}/mod.py "
            f"{layout['plain']}/plain.py",
            layout["project"],
        )
        assert found == [layout["pkg"], layout["plain"]]

    def test_candidate_cap(self, layout):
        cmd = " ".join(f"{layout['pkg']}/mod.py" for _ in range(200))
        assert len(lib.bash_directories(cmd, layout["project"])) <= lib.BASH_MAX_CANDIDATES

    def test_tilde_expansion(self, layout, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "hd").mkdir()
        (tmp_path / "hd" / "f.txt").write_text("x")
        assert lib.bash_directories("cat ~/hd/f.txt", layout["project"]) == [
            os.path.realpath(tmp_path / "hd")
        ]


# ---------------------------------------------------------------------------
# Core contract
# ---------------------------------------------------------------------------

class TestDiscovery:
    """A directory reached only through Bash must surface its CLAUDE.md."""

    def test_bash_into_nested_dir_flags(self, session, layout):
        assert session.discover(f"cat {layout['pkg']}/mod.py") == [layout["pkg_md"]]

    def test_bash_with_relative_path_flags(self, session, layout):
        assert session.discover("cat pkg/mod.py") == [layout["pkg_md"]]

    def test_walk_collects_ancestors_up_to_root(self, session, layout):
        # pkg/deep has no CLAUDE.md of its own; pkg's applies to work there.
        assert session.discover(f"cat {layout['deep']}/deep.py") == [layout["pkg_md"]]

    def test_directory_without_instruction_file_is_silent(self, session, layout):
        assert session.discover(f"cat {layout['plain']}/plain.py") == []

    def test_outside_project_tree_flags(self, session, layout):
        assert session.discover(f"cat {layout['sibling']}/sib.py") == [layout["sibling_md"]]

    def test_loaded_root_is_never_reflagged(self, session, layout):
        session.bash(f"cat {layout['pkg']}/mod.py")
        assert layout["root_md"] not in session.turn()

    def test_second_touch_is_silent(self, session, layout):
        assert session.discover(f"cat {layout['pkg']}/mod.py") == [layout["pkg_md"]]
        assert session.discover(f"grep -rn x {layout['pkg']}/") == []

    def test_agents_md_flagged_when_no_claude_md(self, session, layout, tmp_path):
        other = Path(layout["ws"]) / "agentsonly"
        other.mkdir()
        (other / "AGENTS.md").write_text("# agents\n")
        (other / "a.py").write_text("a = 1\n")
        assert session.discover(f"cat {other}/a.py") == [os.path.realpath(other / "AGENTS.md")]

    def test_agents_md_skipped_beside_claude_md(self, session, layout):
        (Path(layout["pkg"]) / "AGENTS.md").write_text("# agents\n")
        assert session.discover(f"cat {layout['pkg']}/mod.py") == [layout["pkg_md"]]

    def test_agents_md_disabled_by_env(self, session, layout):
        other = Path(layout["ws"]) / "agentsonly2"
        other.mkdir()
        (other / "AGENTS.md").write_text("# agents\n")
        (other / "a.py").write_text("a = 1\n")
        session.env["CLAUDE_MD_DISCOVERY_AGENTS_MD"] = "0"
        assert session.discover(f"cat {other}/a.py") == []

    def test_claude_local_md_flagged(self, session, layout):
        local = Path(layout["pkg"]) / "CLAUDE.local.md"
        local.write_text("# local pkg rules\n")
        assert sorted(session.discover(f"cat {layout['pkg']}/mod.py")) == sorted(
            [layout["pkg_md"], os.path.realpath(local)]
        )


class TestGraceWindow:
    """Emission waits only when a load for that directory may be in flight.

    The window guards one race: a file tool and a Bash call reaching the
    same directory close together, where the file tool's
    InstructionsLoaded is still pending. When nothing could produce such
    an event, waiting is pure cost — and worse than cost, because a turn
    is often a single tool call followed by an answer, so a suspicion that
    needed a *later* batch would sit unemitted until the user typed again.
    """

    def test_uncontended_batch_emits_immediately(self, session, layout):
        assert session.bash(f"cat {layout['pkg']}/mod.py") == [layout["pkg_md"]]

    def test_contended_batch_stays_silent(self, session, layout):
        # A Read in the same batch makes Claude Code load pkg/CLAUDE.md;
        # its event may land after this hook returns.
        assert session.tools([
            read_call(f"{layout['pkg']}/mod.py"),
            bash_call(f"wc -l {layout['pkg']}/mod.py"),
        ]) == []

    def test_contended_suspicion_emits_once_grace_elapses(self, session, layout):
        session.tools([
            read_call(f"{layout['pkg']}/mod.py"),
            bash_call(f"wc -l {layout['pkg']}/mod.py"),
        ])
        age_suspicions(session.env, session.sid, lib.GRACE_SECS + 1)
        assert session.tools([bash_call("echo unrelated")]) == [layout["pkg_md"]]

    def test_late_load_within_window_suppresses(self, session, layout):
        # The whole reason the window exists.
        assert session.tools([
            read_call(f"{layout['pkg']}/mod.py"),
            bash_call(f"wc -l {layout['pkg']}/mod.py"),
        ]) == []
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        assert session.turn() == []

    def test_read_below_the_directory_also_contends(self, session, layout):
        # Reading pkg/deep/deep.py loads pkg/CLAUDE.md too, since the
        # traversal walks upward.
        assert session.tools([
            read_call(f"{layout['deep']}/deep.py"),
            bash_call(f"wc -l {layout['pkg']}/mod.py"),
        ]) == []

    def test_read_above_the_directory_does_not_contend(self, session, layout):
        # Reading a file in the project root cannot produce a load for
        # pkg/CLAUDE.md, so there is nothing to wait for.
        (Path(layout["project"]) / "top.py").write_text("t = 1\n")
        assert session.tools([
            read_call(f"{layout['project']}/top.py"),
            bash_call(f"wc -l {layout['pkg']}/mod.py"),
        ]) == [layout["pkg_md"]]

    def test_contention_persists_across_batches(self, session, layout):
        session.tools([read_call(f"{layout['pkg']}/mod.py")])
        assert session.bash(f"wc -l {layout['pkg']}/mod.py") == []

    def test_turn_boundary_forces_emission(self, session, layout):
        session.tools([
            read_call(f"{layout['pkg']}/mod.py"),
            bash_call(f"wc -l {layout['pkg']}/mod.py"),
        ])
        assert session.turn() == [layout["pkg_md"]]

    def test_duplicate_suspicion_not_queued_twice(self, session, layout):
        session.tools([read_call(f"{layout['pkg']}/mod.py")])
        session.bash(f"cat {layout['pkg']}/mod.py")
        session.bash(f"head -1 {layout['pkg']}/mod.py")
        pending = json.loads(
            state_file(session.env, session.sid, ".pending.json").read_text()
        )
        assert len(pending["susp"]) == 1


class TestSuppression:
    """What Claude Code reports loading is never flagged."""

    def test_natively_loaded_nested_file_not_flagged(self, session, layout):
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        assert session.discover(f"cat {layout['pkg']}/mod.py") == []

    def test_path_glob_match_load_suppresses(self, session, layout):
        rules = Path(layout["project"]) / ".claude" / "rules"
        rules.mkdir(parents=True)
        scoped = rules / "scoped.md"
        scoped.write_text("---\npaths:\n  - 'pkg/**'\n---\n# scoped\n")
        session.loaded(os.path.realpath(scoped), "path_glob_match",
                       trigger=f"{layout['pkg']}/mod.py")
        session.bash(f"cat {layout['pkg']}/mod.py")
        assert os.path.realpath(scoped) not in session.turn()

    def test_identical_content_elsewhere_suppressed(self, session, layout):
        # A copied template or a second clone: the content is already in
        # context, so the path being new is not a reason to re-read it.
        twin = Path(layout["ws"]) / "twin"
        twin.mkdir()
        (twin / "CLAUDE.md").write_text(Path(layout["pkg_md"]).read_text())
        (twin / "t.py").write_text("t = 1\n")
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        assert session.discover(f"cat {twin}/t.py") == []

    def test_diverged_copy_still_flags(self, session, layout):
        twin = Path(layout["ws"]) / "twin2"
        twin.mkdir()
        (twin / "CLAUDE.md").write_text("# genuinely different rules\n")
        (twin / "t.py").write_text("t = 1\n")
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        assert session.discover(f"cat {twin}/t.py") == [os.path.realpath(twin / "CLAUDE.md")]

    def test_unloaded_on_disk_copy_does_not_suppress(self, session, layout):
        # Pre-0.5 a project scan recorded every CLAUDE.md on disk as
        # "known content", so an outside file identical to one that was
        # never loaded got suppressed. Only loaded content may suppress.
        twin = Path(layout["ws"]) / "twin3"
        twin.mkdir()
        (twin / "CLAUDE.md").write_text(Path(layout["pkg_md"]).read_text())
        (twin / "t.py").write_text("t = 1\n")
        assert os.path.realpath(twin / "CLAUDE.md") in session.discover(
            f"cat {twin}/t.py"
        )

    def test_suppression_is_logged_with_match(self, session, layout):
        twin = Path(layout["ws"]) / "twin4"
        twin.mkdir()
        (twin / "CLAUDE.md").write_text(Path(layout["pkg_md"]).read_text())
        (twin / "t.py").write_text("t = 1\n")
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        session.bash(f"cat {twin}/t.py")
        session.turn()
        events = [e for e in session.log() if e["event"] == "suppress"]
        assert events and events[0]["matched"] == layout["pkg_md"]

    def test_symlinked_path_spelling_does_not_split_entry(self, session, layout):
        # InstructionsLoaded reports file_path realpath'd while tools report
        # paths as typed; both must fold onto one ledger key.
        link = Path(layout["ws"]) / "link"
        link.symlink_to(layout["pkg"])
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        assert session.discover(f"cat {link}/mod.py") == []


class TestAgentScoping:
    """A subagent's context is not the main agent's."""

    def test_subagent_touch_flags_for_subagent(self, session, layout):
        assert session.discover(f"cat {layout['pkg']}/mod.py", agent="sub-1") == [layout["pkg_md"]]

    def test_subagent_flag_does_not_suppress_main(self, session, layout):
        session.bash(f"cat {layout['pkg']}/mod.py", agent="sub-1")
        session.turn()
        assert session.discover(f"cat {layout['pkg']}/mod.py") == [layout["pkg_md"]]

    def test_main_flag_does_not_suppress_subagent(self, session, layout):
        session.bash(f"cat {layout['pkg']}/mod.py")
        session.turn()
        assert session.discover(f"cat {layout['pkg']}/mod.py", agent="sub-2") == [layout["pkg_md"]]

    def test_subagent_triggered_load_attributed_via_trigger_index(self, session, layout):
        # InstructionsLoaded carries no agent_id; attribution is recovered
        # by joining trigger_file_path against the tool input that caused it.
        session.tools([read_call(f"{layout['pkg']}/mod.py")], agent="sub-3")
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        assert session.discover(f"cat {layout['pkg']}/mod.py", agent="sub-3") == []

    def test_subagent_load_does_not_suppress_for_main(self, session, layout):
        session.tools([read_call(f"{layout['pkg']}/mod.py")], agent="sub-4")
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        assert session.discover(f"cat {layout['pkg']}/mod.py") == [layout["pkg_md"]]

    def test_session_start_loads_are_visible_to_subagents(self, session, layout):
        session.bash(f"cat {layout['pkg']}/mod.py", agent="sub-5")
        assert layout["root_md"] not in session.turn()


class TestCwdChanged:
    """A shell `cd` loads nothing, and needs no path parsing to detect."""

    def test_cd_into_dir_with_claude_md_flags(self, session, layout):
        session.cd(layout["pkg"])
        assert session.turn() == [layout["pkg_md"]]

    def test_cd_into_plain_dir_is_silent(self, session, layout):
        session.cd(layout["plain"])
        assert session.turn() == []

    def test_cd_outside_project_flags(self, session, layout):
        session.cd(layout["sibling"])
        assert session.turn() == [layout["sibling_md"]]

    def test_cd_respects_agent_scope(self, session, layout):
        session.cd(layout["pkg"], agent="sub-cd")
        session.turn()
        session.cd(layout["pkg"])
        assert session.turn() == [layout["pkg_md"]]

    def test_cd_emits_nothing_itself(self, session, layout):
        # CwdChanged has no channel that reaches Claude; it only records.
        code, out = session.cd(layout["pkg"])
        assert (code, out) == (0, "")

    def test_walk_boundary_uses_session_root_not_current_cwd(self, session, layout):
        # `cd` moves the payload cwd, but the ancestor boundary must stay
        # anchored at the session root or a cd into pkg/deep would treat
        # pkg as an already-loaded ancestor.
        session.cd(layout["deep"])
        assert session.turn() == [layout["pkg_md"]]


class TestWorktree:
    """A worktree switch invalidates lazily loaded files."""

    def _worktree(self, layout):
        wt = Path(layout["project"]) / ".claude" / "worktrees" / "wt"
        (wt / "pkg").mkdir(parents=True)
        (wt / "CLAUDE.md").write_text("# root rules\n")
        (wt / "pkg" / "CLAUDE.md").write_text("# pkg rules\n")
        (wt / "pkg" / "mod.py").write_text("x = 1\n")
        return wt

    def test_enter_worktree_drops_lazy_loads(self, session, layout):
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        wt = self._worktree(layout)
        session.tools([{"tool_name": "EnterWorktree", "tool_input": {"name": "wt"}}],
                      cwd=str(wt))
        session.cwd = os.path.realpath(wt)
        assert layout["pkg_md"] in session.discover(f"cat {layout['pkg']}/mod.py")

    def test_enter_worktree_keeps_session_start_loads(self, session, layout):
        wt = self._worktree(layout)
        session.tools([{"tool_name": "EnterWorktree", "tool_input": {"name": "wt"}}],
                      cwd=str(wt))
        session.cwd = os.path.realpath(wt)
        session.bash(f"cat {layout['pkg']}/mod.py")
        assert layout["root_md"] not in session.turn()

    def test_worktree_switch_is_logged(self, session, layout):
        wt = self._worktree(layout)
        session.tools([{"tool_name": "EnterWorktree", "tool_input": {"name": "wt"}}],
                      cwd=str(wt))
        assert any(e["event"] == "worktree_switch" for e in session.log())

    def test_plain_cd_does_not_drop_loads(self, session, layout):
        # Only the worktree tools invalidate; a cd moves cwd too and must
        # not be mistaken for a checkout change.
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        session.tools([bash_call("cd pkg && pwd")], cwd=layout["pkg"])
        session.bash(f"cat {layout['pkg']}/mod.py", cwd=layout["pkg"])
        assert session.turn() == []

    def test_worktree_tool_without_cwd_change_is_noop(self, session, layout):
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        session.tools([{"tool_name": "ExitWorktree", "tool_input": {"action": "keep"}}])
        assert session.discover(f"cat {layout['pkg']}/mod.py") == []


class TestLifecycle:
    """`/clear`, compaction, resume, and garbage collection."""

    def test_clear_resets_everything(self, session, layout):
        session.bash(f"cat {layout['pkg']}/mod.py")
        session.turn()
        session.start(source="clear")
        session.loaded(layout["root_md"])
        assert session.discover(f"cat {layout['pkg']}/mod.py") == [layout["pkg_md"]]

    def test_compact_drops_flags_keeps_loads(self, session, layout):
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        outside = Path(layout["sibling"])
        assert session.discover(f"cat {outside}/sib.py") == [layout["sibling_md"]]
        session.start(source="compact")
        # The flagged file lived only in the transcript, so it re-flags.
        assert session.discover(f"cat {outside}/sib.py") == [layout["sibling_md"]]
        # The natively loaded one did not.
        assert session.discover(f"cat {layout['pkg']}/mod.py") == []

    def test_compact_drop_is_logged(self, session, layout):
        session.bash(f"cat {layout['sibling']}/sib.py")
        session.turn()
        session.start(source="compact")
        assert any(e["event"] == "compact_drop" for e in session.log())

    def test_resume_keeps_state(self, session, layout):
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        session.start(source="resume")
        assert session.discover(f"cat {layout['pkg']}/mod.py") == []

    def test_session_end_clear_deletes_state(self, session, layout):
        session.bash(f"cat {layout['pkg']}/mod.py")
        session.turn()
        run_script(SESSION_END,
                   {"session_id": session.sid, "reason": "clear"}, session.env)
        assert not state_file(session.env, session.sid, ".jsonl").exists()

    def test_session_end_other_reason_keeps_state(self, session, layout):
        session.bash(f"cat {layout['pkg']}/mod.py")
        session.turn()
        run_script(SESSION_END,
                   {"session_id": session.sid, "reason": "exit"}, session.env)
        assert state_file(session.env, session.sid, ".jsonl").exists()

    def test_gc_removes_stale_state(self, session, layout):
        # A session that crashed without firing SessionEnd leaves state
        # behind; the next session's SessionStart sweeps it.
        stale = state_file(session.env, next_sid("dead"), ".jsonl")
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text('{"t":"load","p":"/x/CLAUDE.md","h":"h","r":"","g":""}\n')
        old = time.time() - (lib.STATE_MAX_AGE_DAYS + 1) * 86400
        os.utime(stale, (old, old))
        session.start()
        assert not stale.exists()

    def test_gc_keeps_fresh_state(self, session, layout):
        session.bash(f"cat {layout['pkg']}/mod.py")
        session.turn()
        session.start()
        assert state_file(session.env, session.sid, ".jsonl").exists()

    def test_plugin_enabled_mid_session_still_works(self, hook_env, layout):
        # No SessionStart ran, so there is no recorded root; the payload
        # cwd has to stand in for it.
        sid = next_sid("late")
        code, out = run_script(
            ON_BATCH,
            batch(sid, layout["project"], [bash_call(f"cat {layout['pkg']}/mod.py")]),
            hook_env,
        )
        assert code == 0
        assert flagged_paths(out) == [layout["pkg_md"]]


class TestIgnoreList:
    """`CLAUDE_MD_DISCOVERY_IGNORE` makes paths invisible to the plugin."""

    def test_ignored_prefix_never_flags(self, session, layout):
        session.env["CLAUDE_MD_DISCOVERY_IGNORE"] = layout["sibling"]
        assert session.discover(f"cat {layout['sibling']}/sib.py") == []

    def test_multiple_prefixes(self, session, layout):
        session.env["CLAUDE_MD_DISCOVERY_IGNORE"] = os.pathsep.join(
            [layout["sibling"], layout["pkg"]]
        )
        session.bash(f"cat {layout['sibling']}/sib.py")
        assert session.discover(f"cat {layout['pkg']}/mod.py") == []

    def test_root_prefix_is_kill_switch(self, session, layout):
        session.env["CLAUDE_MD_DISCOVERY_IGNORE"] = "/"
        session.bash(f"cat {layout['pkg']}/mod.py")
        assert session.discover(f"cat {layout['sibling']}/sib.py") == []

    def test_unignored_sibling_still_flags(self, session, layout):
        session.env["CLAUDE_MD_DISCOVERY_IGNORE"] = layout["plain"]
        assert session.discover(f"cat {layout['pkg']}/mod.py") == [layout["pkg_md"]]


class TestConfigDirExclusion:
    """Claude Code's own config tree is never a discovery target."""

    def test_config_dir_claude_md_not_flagged(self, session, hook_env):
        config = Path(hook_env["CLAUDE_CONFIG_DIR"])
        (config / "CLAUDE.md").write_text("# global user memory\n")
        (config / "notes.txt").write_text("x\n")
        assert session.discover(f"cat {config}/notes.txt") == []

    def test_plugin_claude_md_under_config_not_flagged(self, session, hook_env):
        plugin = Path(hook_env["CLAUDE_CONFIG_DIR"]) / "plugins" / "somewhere"
        plugin.mkdir(parents=True)
        (plugin / "CLAUDE.md").write_text("# plugin rules\n")
        (plugin / "code.py").write_text("x = 1\n")
        assert session.discover(f"cat {plugin}/code.py") == []

    def test_cd_into_config_dir_not_flagged(self, session, hook_env):
        config = Path(hook_env["CLAUDE_CONFIG_DIR"])
        (config / "CLAUDE.md").write_text("# global\n")
        session.cd(str(config))
        assert session.turn() == []

    def test_sibling_still_flags_with_config_dir_set(self, session, layout):
        assert session.discover(f"cat {layout['sibling']}/sib.py") == [layout["sibling_md"]]


class TestOutputFormat:
    """Findings are delivered as additionalContext, never as a block."""

    def _context(self, session, layout):
        return json.loads(collect(session, f"cat {layout['pkg']}/mod.py"))

    def test_uses_hook_specific_output(self, session, layout):
        payload = self._context(session, layout)
        assert payload["hookSpecificOutput"]["hookEventName"] in (
            "PostToolBatch", "UserPromptSubmit"
        )
        assert "additionalContext" in payload["hookSpecificOutput"]

    def test_does_not_block(self, session, layout):
        payload = self._context(session, layout)
        assert "decision" not in payload
        assert "continue" not in payload

    def test_message_is_wrapped_in_tags(self, session, layout):
        text = self._context(session, layout)["hookSpecificOutput"]["additionalContext"]
        assert text.startswith("<claude-md-discovery-extended>")
        assert text.endswith("</claude-md-discovery-extended>")

    def test_message_names_the_read_tool(self, session, layout):
        text = self._context(session, layout)["hookSpecificOutput"]["additionalContext"]
        assert "Read tool" in text

    def test_message_mentions_imports(self, session, layout):
        text = self._context(session, layout)["hookSpecificOutput"]["additionalContext"]
        assert "@path" in text

    def test_batch_output_names_its_own_event(self, session, layout):
        _, out = run_script(
            ON_BATCH,
            batch(session.sid, session.cwd,
                  [bash_call(f"cat {layout['pkg']}/mod.py")]),
            session.env,
        )
        assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "PostToolBatch"

    def test_turn_output_names_its_own_event(self, session, layout):
        session.tools([
            read_call(f"{layout['pkg']}/mod.py"),
            bash_call(f"wc -l {layout['pkg']}/mod.py"),
        ])
        _, out = run_script(
            ON_PROMPT, {"session_id": session.sid, "cwd": session.cwd}, session.env
        )
        assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

    def test_single_and_multiple_forms_both_parse(self, session, layout):
        assert len(session.discover(f"cat {layout['pkg']}/mod.py")) == 1
        other = Path(layout["ws"]) / "third"
        other.mkdir()
        (other / "CLAUDE.md").write_text("# third\n")
        (other / "t.py").write_text("t = 1\n")
        _, out = run_script(
            ON_BATCH,
            batch(session.sid, session.cwd, [
                bash_call(f"cat {layout['sibling']}/sib.py"),
                bash_call(f"cat {other}/t.py"),
            ]),
            session.env,
        )
        assert len(flagged_paths(out)) == 2


class TestDiagnostics:
    """The event log must explain every decision without being chatty."""

    def test_flag_event_logged(self, session, layout):
        session.bash(f"cat {layout['pkg']}/mod.py")
        session.turn()
        flags = [e for e in session.log() if e["event"] == "flag"]
        assert flags and flags[0]["inlined"] == [layout["pkg_md"]]

    def test_steady_state_logs_nothing(self, session, layout):
        before = len(session.log())
        session.bash(f"cat {layout['plain']}/plain.py")
        session.turn()
        assert len(session.log()) == before

    def test_exception_is_logged_with_traceback(self, session, hook_env, monkeypatch):
        env = dict(hook_env)
        env["CLAUDE_MD_DISCOVERY_STATE_DIR"] = hook_env["CLAUDE_MD_DISCOVERY_STATE_DIR"]
        pending = state_file(env, session.sid, ".pending.json")
        pending.parent.mkdir(parents=True, exist_ok=True)
        # A ledger line that is valid JSON but the wrong shape must not
        # take the hook down; a genuine crash must leave a trace.
        ledger = state_file(env, session.sid, ".jsonl")
        ledger.write_text("not json at all\n{}\n")
        code, _ = run_script(
            ON_BATCH, batch(session.sid, session.cwd, [bash_call("echo x")]), env
        )
        assert code == 0

    def test_log_is_capped(self, session):
        path = state_file(session.env, session.sid, ".log")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * (lib.LOG_MAX_BYTES + 1))
        size = path.stat().st_size
        lib.log_event  # noqa: B018 - documented cap, exercised via the script
        session.bash("echo x")
        assert path.stat().st_size == size


class TestChangeDetection:
    """A CLAUDE.md edited mid-session leaves the model holding stale rules."""

    def _changed(self, stdout: str) -> list[str]:
        """Extract the stale-file list from a hook's emitted context."""
        return parse_message(stdout)["changed"]

    def _turn_raw(self, session):
        _, out = run_script(
            ON_PROMPT, {"session_id": session.sid, "cwd": session.cwd}, session.env
        )
        return out

    def test_edited_loaded_file_prompts_reread(self, session, layout):
        Path(layout["root_md"]).write_text("# root rules, revised\n")
        assert self._changed(self._turn_raw(session)) == [layout["root_md"]]

    def test_unchanged_file_is_silent(self, session, layout):
        assert self._changed(self._turn_raw(session)) == []

    def test_change_reported_once(self, session, layout):
        Path(layout["root_md"]).write_text("# root rules, revised\n")
        assert self._changed(self._turn_raw(session)) == [layout["root_md"]]
        assert self._changed(self._turn_raw(session)) == []

    def test_nested_loaded_file_change_detected(self, session, layout):
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        Path(layout["pkg_md"]).write_text("# pkg rules, revised\n")
        assert self._changed(self._turn_raw(session)) == [layout["pkg_md"]]

    def test_model_write_does_not_nag(self, session, layout):
        # The model edited the file itself, so the new content is already
        # in its context.
        Path(layout["root_md"]).write_text("# root rules, revised by model\n")
        session.tools([{"tool_name": "Edit",
                        "tool_input": {"file_path": layout["root_md"]}}])
        assert self._changed(self._turn_raw(session)) == []

    def test_ignored_file_change_not_reported(self, session, layout):
        session.env["CLAUDE_MD_DISCOVERY_IGNORE"] = layout["project"]
        Path(layout["root_md"]).write_text("# revised\n")
        assert self._changed(self._turn_raw(session)) == []

    def test_deleted_file_is_not_reported(self, session, layout):
        Path(layout["root_md"]).unlink()
        assert self._changed(self._turn_raw(session)) == []

    def test_change_and_discovery_coexist(self, session, layout):
        Path(layout["root_md"]).write_text("# root rules, revised\n")
        out = collect(session, f"cat {layout['pkg']}/mod.py")
        assert flagged_paths(out) == [layout["pkg_md"]]
        assert self._changed(out) == [layout["root_md"]]

    def test_change_is_caught_on_the_tool_path(self, session, layout):
        # Not only at turn boundaries: an edit made while the user watches
        # a long run of tool calls must surface during that run.
        Path(layout["root_md"]).write_text("# root rules, revised\n")
        _, out = run_script(
            ON_BATCH, batch(session.sid, session.cwd, [bash_call("echo hi")]),
            session.env,
        )
        assert self._changed(out) == [layout["root_md"]]

    def test_change_check_is_throttled(self, session, layout):
        Path(layout["root_md"]).write_text("# revised once\n")
        _, out = run_script(
            ON_BATCH, batch(session.sid, session.cwd, [bash_call("echo a")]),
            session.env,
        )
        assert self._changed(out) == [layout["root_md"]]
        Path(layout["root_md"]).write_text("# revised twice\n")
        _, out = run_script(
            ON_BATCH, batch(session.sid, session.cwd, [bash_call("echo b")]),
            session.env,
        )
        assert self._changed(out) == []


class TestDirectAccess:
    """Reading a CLAUDE.md yourself must not earn you a nag to read it again.

    Claude Code suppresses its native memory load when the file being read
    *is* the memory file, so no `InstructionsLoaded` arrives and the plugin
    has to notice the read itself. Verified live: a full Read of a
    CLAUDE.md produced no load event, and the directory was then flagged on
    the next Bash touch.
    """

    def test_read_of_claude_md_suppresses_later_flag(self, session, layout):
        session.tools([read_call(layout["pkg_md"])])
        assert session.discover(f"cat {layout['pkg']}/mod.py") == []

    def test_partial_read_does_not_suppress(self, session, layout):
        # A `limit=1` read returns one line and loads nothing else, so the
        # model does not actually have the rules.
        session.tools([{"tool_name": "Read",
                        "tool_input": {"file_path": layout["pkg_md"], "limit": 1}}])
        assert session.discover(f"cat {layout['pkg']}/mod.py") == [layout["pkg_md"]]

    def test_offset_read_does_not_suppress(self, session, layout):
        session.tools([{"tool_name": "Read",
                        "tool_input": {"file_path": layout["pkg_md"], "offset": 2}}])
        assert session.discover(f"cat {layout['pkg']}/mod.py") == [layout["pkg_md"]]

    def test_write_of_new_claude_md_suppresses(self, session, layout):
        target = Path(layout["plain"]) / "CLAUDE.md"
        target.write_text("# freshly authored\n")
        session.tools([{"tool_name": "Write",
                        "tool_input": {"file_path": str(target)}}])
        assert session.discover(f"cat {layout['plain']}/plain.py") == []

    def test_read_is_scoped_to_the_reading_agent(self, session, layout):
        session.tools([read_call(layout["pkg_md"])], agent="sub-r")
        assert session.discover(f"cat {layout['pkg']}/mod.py") == [layout["pkg_md"]]

    def test_read_of_non_instruction_file_does_not_suppress(self, session, layout):
        session.tools([read_call(f"{layout['pkg']}/mod.py")])
        assert session.discover(f"cat {layout['pkg']}/mod.py") == [layout["pkg_md"]]

    def test_direct_read_is_dropped_on_compaction(self, session, layout):
        # The copy lived only in the transcript, which compaction drops.
        session.tools([read_call(layout["pkg_md"])])
        assert session.discover(f"cat {layout['pkg']}/mod.py") == []
        session.start(source="compact")
        assert session.discover(f"cat {layout['pkg']}/mod.py") == [layout["pkg_md"]]


class TestPseudoFilesystems:
    """Redirects to /dev/null must not register as directory touches."""

    @pytest.mark.parametrize("command", [
        "grep -rn x pkg/ 2>/dev/null",
        "find . -name '*.py' 2>/dev/null",
        "cat /dev/null",
    ])
    def test_dev_null_is_not_a_candidate(self, layout, command):
        assert "/dev" not in lib.bash_directories(command, layout["project"])

    def test_real_path_still_found_alongside_dev_null(self, layout):
        found = lib.bash_directories("grep -rn x pkg/ 2>/dev/null", layout["project"])
        assert found == [layout["pkg"]]


class TestWorktreeKeepsTranscript:
    """A worktree switch clears memory caches, not the transcript."""

    def _worktree(self, layout):
        wt = Path(layout["project"]) / ".claude" / "worktrees" / "wt"
        (wt / "pkg").mkdir(parents=True)
        (wt / "pkg" / "mod.py").write_text("x = 1\n")
        return wt

    def _switch(self, session, layout):
        wt = self._worktree(layout)
        session.tools([{"tool_name": "EnterWorktree", "tool_input": {"name": "wt"}}],
                      cwd=str(wt))
        session.cwd = os.path.realpath(wt)
        return wt

    def test_read_file_not_reflagged_after_switch(self, session, layout):
        # The model read this file; changing directories does not remove
        # it from the transcript, so it must not be surfaced again.
        session.tools([read_call(layout["pkg_md"])])
        self._switch(session, layout)
        assert session.discover(f"cat {layout['pkg']}/mod.py") == []

    def test_flagged_file_not_reflagged_after_switch(self, session, layout):
        assert session.discover(f"cat {layout['sibling']}/sib.py") == [layout["sibling_md"]]
        self._switch(session, layout)
        assert session.discover(f"cat {layout['sibling']}/sib.py") == []

    def test_native_load_is_still_dropped_after_switch(self, session, layout):
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        self._switch(session, layout)
        assert session.discover(f"cat {layout['pkg']}/mod.py") == [layout["pkg_md"]]


class TestInlining:
    """Contents are delivered, not announced.

    Announcing costs a round trip, depends on the model complying, and
    lands the text as a line-numbered tool result. Inlining puts it in the
    same `<system-reminder>` framing Claude Code uses for memory it loads
    itself — confirmed from the transcript, where a `nested_memory`
    attachment and a `hook_additional_context` attachment share the
    wrapper and both fold into the user turn.
    """

    def _raw(self, session, layout):
        return collect(session, f"cat {layout['pkg']}/mod.py")

    def test_content_is_inlined(self, session, layout):
        text = inlined_content(self._raw(session, layout))
        assert "# pkg rules" in text

    def test_uses_native_contents_header(self, session, layout):
        text = inlined_content(self._raw(session, layout))
        assert f"Contents of {layout['pkg_md']}:" in text

    def test_no_read_instruction_when_inlined(self, session, layout):
        text = inlined_content(self._raw(session, layout))
        assert "too large to include inline" not in text

    def test_path_is_still_reported(self, session, layout):
        assert flagged_paths(self._raw(session, layout)) == [layout["pkg_md"]]

    def test_oversized_file_falls_back_to_read(self, session, layout):
        Path(layout["pkg_md"]).write_text("# big\n" + "x" * (lib.INLINE_MAX_FILE + 10))
        out = self._raw(session, layout)
        parsed = parse_message(out)
        assert parsed["overflow"] == [layout["pkg_md"]]
        assert parsed["new"] == []
        assert "Read tool" in inlined_content(out)

    def test_unreadable_file_falls_back_to_read(self, session, layout):
        Path(layout["pkg_md"]).write_bytes(b"\xff\xfe\x00binary\x00")
        parsed = parse_message(self._raw(session, layout))
        assert parsed["overflow"] == [layout["pkg_md"]]

    def test_budget_spills_later_files_to_read(self, session, layout):
        # Three files that individually fit but together do not.
        size = lib.INLINE_BUDGET // 2
        calls = []
        for i in range(3):
            d = Path(layout["ws"]) / f"big{i}"
            d.mkdir()
            (d / "CLAUDE.md").write_text(f"# big{i}\n" + "y" * size)
            (d / "f.py").write_text("f = 1\n")
            calls.append(bash_call(f"cat {d}/f.py"))
        _, out = run_script(
            ON_BATCH, batch(session.sid, session.cwd, calls), session.env
        )
        parsed = parse_message(out)
        assert parsed["new"], "at least one file should inline"
        assert parsed["overflow"], "the rest should spill to the read path"

    def test_message_stays_under_hook_output_cap(self, session, layout):
        size = lib.INLINE_BUDGET // 2
        calls = []
        for i in range(4):
            d = Path(layout["ws"]) / f"cap{i}"
            d.mkdir()
            (d / "CLAUDE.md").write_text(f"# cap{i}\n" + "z" * size)
            (d / "f.py").write_text("f = 1\n")
            calls.append(bash_call(f"cat {d}/f.py"))
        _, out = run_script(
            ON_BATCH, batch(session.sid, session.cwd, calls), session.env
        )
        assert len(inlined_content(out)) < 10000

    def test_changed_file_inlines_current_content(self, session, layout):
        Path(layout["root_md"]).write_text("# root rules, revised\nNEW_RULE_MARKER\n")
        _, out = run_script(
            ON_PROMPT, {"session_id": session.sid, "cwd": session.cwd}, session.env
        )
        assert parse_message(out)["changed"] == [layout["root_md"]]
        assert "NEW_RULE_MARKER" in inlined_content(out)

    def test_new_and_changed_sections_are_distinct(self, session, layout):
        Path(layout["root_md"]).write_text("# root revised\nROOT_MARKER\n")
        out = self._raw(session, layout)
        parsed = parse_message(out)
        assert parsed["new"] == [layout["pkg_md"]]
        assert parsed["changed"] == [layout["root_md"]]

    def test_still_records_flag_so_it_emits_once(self, session, layout):
        assert flagged_paths(self._raw(session, layout)) == [layout["pkg_md"]]
        assert session.discover(f"grep -rn x {layout['pkg']}/") == []


class TestAnnouncedIsNotDelivered:
    """A file too large to inline is announced, and announcing delivers nothing.

    Found in a live session: the plugin announced a large CLAUDE.md,
    recorded it as handled, and never surfaced it again — while its
    contents had never entered the context window at all.
    """

    def _oversize(self, layout):
        Path(layout["pkg_md"]).write_text("# big\n" + "x" * (lib.INLINE_MAX_FILE + 10))

    def test_oversized_file_is_announced_not_inlined(self, session, layout):
        self._oversize(layout)
        parsed = parse_message(self._touch(session, layout))
        assert parsed["overflow"] == [layout["pkg_md"]]
        assert parsed["new"] == []

    def _raw_turn(self, session):
        _, out = run_script(
            ON_PROMPT, {"session_id": session.sid, "cwd": session.cwd}, session.env
        )
        return out

    def _touch(self, session, layout, command=None):
        return collect(session, command or f"cat {layout['pkg']}/mod.py")

    def test_announcement_is_not_recorded_as_known(self, session, layout):
        self._oversize(layout)
        self._touch(session, layout)
        ledger = state_file(session.env, session.sid, ".jsonl").read_text()
        records = [json.loads(ln) for ln in ledger.splitlines() if ln.strip()]
        kinds = {o["t"] for o in records if o.get("p") == layout["pkg_md"]}
        assert "ann" in kinds
        assert "flag" not in kinds

    def test_unread_announcement_surfaces_again(self, session, layout):
        self._oversize(layout)
        assert parse_message(self._touch(session, layout))["overflow"] == [
            layout["pkg_md"]
        ]
        backdate_announcements(session.env, session.sid)
        assert parse_message(
            self._touch(session, layout, f"grep -rn x {layout['pkg']}/")
        )["overflow"] == [layout["pkg_md"]]

    def test_repeat_is_rate_limited(self, session, layout):
        self._oversize(layout)
        assert parse_message(self._touch(session, layout))["overflow"] == [
            layout["pkg_md"]
        ]
        assert parse_message(
            self._touch(session, layout, f"grep -rn x {layout['pkg']}/")
        )["overflow"] == []

    def test_reading_it_stops_the_announcements(self, session, layout):
        self._oversize(layout)
        self._touch(session, layout)
        session.tools([read_call(layout["pkg_md"])])
        backdate_announcements(session.env, session.sid)
        assert parse_message(
            self._touch(session, layout, f"grep -rn x {layout['pkg']}/")
        )["overflow"] == []

    def test_oversized_change_is_not_marked_current(self, session, layout):
        # A changed file too large to inline must keep reporting, or the
        # model silently keeps working from the superseded version.
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        self._oversize(layout)
        assert parse_message(self._raw_turn(session))["overflow"] == [layout["pkg_md"]]
        backdate_announcements(session.env, session.sid)
        assert parse_message(self._raw_turn(session))["overflow"] == [layout["pkg_md"]]

    def test_inlined_change_is_marked_current(self, session, layout):
        session.loaded(layout["pkg_md"], "nested_traversal",
                       trigger=f"{layout['pkg']}/mod.py")
        Path(layout["pkg_md"]).write_text("# pkg rules, revised\n")
        assert parse_message(self._raw_turn(session))["changed"] == [layout["pkg_md"]]
        assert parse_message(self._raw_turn(session))["changed"] == []


class TestMessageDoesNotHideFromUser:
    """The injected text must not tell the model to withhold from the user."""

    def _text(self, session, layout):
        return inlined_content(collect(session, f"cat {layout['pkg']}/mod.py"))

    def test_no_instruction_to_withhold(self, session, layout):
        text = self._text(session, layout)
        assert "Do NOT stop to tell the user" not in text
        assert "Do not stop to inform the user" not in text

    def test_directs_honest_answers_about_loaded_instructions(self, session, layout):
        assert "answer honestly" in self._text(session, layout)

    def test_still_says_not_to_derail_the_task(self, session, layout):
        assert "rather than pausing to report" in self._text(session, layout)
