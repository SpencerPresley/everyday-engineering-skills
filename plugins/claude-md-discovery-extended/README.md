# claude-md-discovery-extended

Claude Code loads a nested `CLAUDE.md` when a **file tool** reaches into its directory. A **Bash** command reaching the same directory loads nothing. If you work mostly through `cat`, `sed`, `grep`, and in-place shell edits — which agents increasingly do — your nested instruction files silently never enter the context window.

This plugin closes that gap. It tracks what Claude Code *actually* loaded, watches Bash and `cd` for directories Claude Code missed, and tells Claude to read the difference.

## The gap, measured

A probe session in a fixture repo with a `CLAUDE.md` in three sibling directories, one access each:

```text
TOOL Read   repo/readdir/target_read.py
TOOL Bash   cat repo/catdir/target_cat.py
TOOL Bash   grep -rn needle repo/greponly
IL  nested_traversal  repo/readdir/CLAUDE.md   trigger=repo/readdir/target_read.py
```

One `InstructionsLoaded` event. `catdir/CLAUDE.md` and `greponly/CLAUDE.md` never loaded. A `cd` into a directory is no better: the session's working directory moved into `greponly/` and its `CLAUDE.md` still never loaded.

## How it works

### What's loaded is observed, not inferred

The [`InstructionsLoaded`](https://code.claude.com/docs/en/hooks#instructionsloaded) hook fires once per instruction file Claude Code loads, reporting `file_path`, `memory_type` (`User` / `Project` / `Local` / `Managed`), `load_reason`, and for lazy loads the `trigger_file_path` that caused it. That is ground truth, and it is the plugin's only source for "in context."

This covers things a hand-rolled ancestor walk gets wrong: `.claude/rules/*.md` (including `paths:`-scoped rules, which arrive as `path_glob_match`), `CLAUDE.local.md`, `.claude/CLAUDE.md`, `@path` import expansion, managed-policy files, and the re-injection that follows compaction.

There is one blind spot, and the plugin handles it separately. Reading a `CLAUDE.md` **with the Read tool** fires no event: Claude Code suppresses its native memory load when the file being read *is* the memory file, so the content reaches the transcript without a load. The plugin therefore records direct reads itself — as a flag, not a load, since that is what it is: transcript-carried, scoped to the agent that read it, and dropped on compaction. Without this, reading a `CLAUDE.md` and then touching its directory with Bash would tell you to read a file you just read, putting a second copy in context.

A **partial** read (`offset`/`limit`) doesn't count. It returns the requested lines and loads nothing else, so the model doesn't actually have the rules — reading line 1 to "trigger" the loader does not work.

### Bash directories are the only guess

For a `Bash` call, the plugin tokenizes the command and keeps tokens that resolve to something on disk — absolute, `~`-prefixed, or **relative to the session's working directory**. Everything else (flags, `sed` scripts, bare subcommands, heredoc text) falls away because it doesn't resolve. Redirect targets under `/dev`, `/proc`, and `/sys` are dropped explicitly: `2>/dev/null` appears in a large share of commands and always resolves.

This is best-effort by construction, and the failure modes are asymmetric on purpose:

- **Missed path** → silence, which is exactly where you'd be without the plugin. No regression.
- **Spurious path** → one instruction file you didn't strictly need. Cheap.

`CwdChanged` covers the `cd`-then-work pattern with no parsing at all: `new_cwd` is exact.

### Nothing is emitted synchronously

`InstructionsLoaded` is asynchronous, and one event was measured arriving **24ms after the `PostToolBatch` for the very batch that caused it**. So a batch holding both a `Read` of `pkg/mod.py` and a `Bash` call touching `pkg/` would flag `pkg/CLAUDE.md` while Claude Code was still loading it.

The window is 3s, sized against `nested_traversal` latency (0.033s–1.413s observed), which is the only load reason that can fire for a file this plugin would flag. It only delays: the turn-boundary backstop forces everything pending, so nothing is dropped.

So a Bash touch records a *suspicion*, and the plugin asks whether anything could actually be racing it. A file tool at that directory **or beneath it** puts a load in flight, because the traversal walks upward; a file tool above it cannot. Only a contended suspicion waits — an uncontended one is emitted by the very `PostToolBatch` that observed it.

That distinction is what makes the plugin usable rather than merely correct. A turn is very often a single tool call followed by an answer, and a suspicion that always needed a *later* batch would never be emitted during that turn at all — it would sit until the user happened to type again. If you have to prompt the model to make the hook fire, you may as well have told it to read the file yourself.

Nothing blocks: `PostToolUse`-family exit code 2 isn't honored anyway, so findings go out as `additionalContext`.

### Contents are delivered, not announced

The plugin inlines the instruction file rather than telling Claude to go read it. That choice comes from the transcript format. A natively loaded memory file arrives as:

```json
{ "type": "attachment",
  "attachment": { "type": "nested_memory", "path": ".../CLAUDE.md" },
  "rendered": [{ "content": "<system-reminder>\nContents of .../CLAUDE.md:\n\n...\n</system-reminder>" }] }
```

and hook `additionalContext` arrives as the *same class of record* — `attachment.type: "hook_additional_context"`, wrapped in the same `<system-reminder>`, folded into the same user turn. So inlining reaches the model in the framing it already associates with project instructions, while a `Read` would deliver it as a line-numbered tool result. Inlining also costs no round trip and doesn't depend on Claude complying.

For the same reason the payload carries **no wrapper tag of its own** and begins with a newline. Claude Code already encloses it in `<system-reminder>` and prefixes it with `<event> hook additional context:`; a second nested tag added noise, and without the leading newline the first sentence ran on from that prefix.

Files over 6,000 characters fall back to an *announcement* — the path plus an instruction to read it — because hook output is capped at 10,000 characters, past which Claude Code spills it to a file and substitutes a preview. The whole message is budgeted under that cap, spilling later findings to the announcement list rather than truncating.

**Announcing is not delivering, and the ledger tracks the difference.** An inlined file's contents are in context, so it is marked known and never surfaces again. An announced file's contents are not, so it is recorded only as an announcement: it surfaces again on a later touch of that directory (rate-limited to once per five minutes) and stops only when the model is observed actually reading it. The same rule applies to a changed file too large to inline — its new hash is not committed until the new content has been delivered, so it keeps reporting rather than silently marking itself current.

An inlined finding is emitted once per agent per context window: re-emission is gated on `(path, agent)`, identical content at other paths is suppressed by hash, and the gate is only released where the content genuinely leaves context — compaction and `/clear`.

### Deduplication by content

Suppression is by content hash, not path, so byte-identical copies across git worktrees, extra clones, and copied templates flag once rather than N times. Only content Claude Code reported loading (or that the plugin already flagged) can suppress — a file merely sitting on disk never does.

### Per-agent scoping

`InstructionsLoaded` carries no `agent_id` even when a subagent's tool call caused the load, but tool events do, and `trigger_file_path` matches the triggering tool's `tool_input.file_path` byte for byte. The plugin joins on that to recover attribution, resolving it lazily at emit time so it doesn't matter whether the load or the tool batch is recorded first.

A file loaded inside a subagent therefore doesn't suppress discovery for the main agent, which never saw it. Loads with reason `session_start` or `compact` are treated as visible to every agent.

### Staleness

Claude Code loads a memory file once and never reloads it, so editing a `CLAUDE.md` mid-session leaves the model working from rules it can no longer see. Every loaded file is re-hashed on the tool path (throttled to once every 15s) and again at each turn boundary, and a change delivers the current content. Running it only at turn boundaries would have the same defect as deferred discovery: an edit made while you watch a long run of tool calls wouldn't surface until you next typed. Edits Claude makes itself through `Write`/`Edit` don't nag — that content is already in its context.

### Lifecycle

- **`/clear`** wipes the context, so all state is deleted; Claude Code re-fires `session_start` loads, which reseeds it for free.
- **Compaction** drops transcript-only content, so plugin-flagged files are forgotten and may re-flag. Natively loaded files are re-reported with `load_reason: compact`.
- **Worktree switches** (`EnterWorktree` / `ExitWorktree`) clear Claude Code's memory-file caches and move the session into a different checkout, so lazily *loaded* files are forgotten. Files the model **read** are not: a transcript isn't cleared by changing directories, so that content is still in context. Detected by tool name, not by a `cwd` change — a plain `cd` moves `cwd` too and must not invalidate anything.
- **Resume** keeps state, so a resumed session isn't re-nagged about files in its restored context.

## Hooks

| Event | Role |
| :--- | :--- |
| `InstructionsLoaded` | Records every file Claude Code loaded. The only writer of "in context." |
| `PostToolBatch` | Extracts Bash directories, indexes triggers and file-tool touches, emits ready findings and throttled staleness checks. Once per batch, not per tool. |
| `CwdChanged` | Records a `cd` destination as touched. |
| `UserPromptSubmit` | Turn-boundary flush: forces every pending finding, including contended ones still inside their window. |
| `SessionStart` | Anchors the session root, handles `/clear` and compaction, garbage-collects abandoned state. |
| `SessionEnd` | Deletes state on `/clear`; keeps it otherwise. |

## Configuration

- `CLAUDE_MD_DISCOVERY_AGENTS_MD=0` — disable `AGENTS.md` discovery. (Claude Code never loads `AGENTS.md` natively; the plugin surfaces it only where no `CLAUDE.md` sits beside it.)
- `CLAUDE_MD_DISCOVERY_IGNORE=/path/one:/path/two` — path prefixes the plugin treats as invisible. `/` disables the plugin entirely.
- `CLAUDE_MD_DISCOVERY_STATE_DIR=/path` — override the state directory.

## Diagnostics

State lives in `~/.claude/plugin-state/claude-md-discovery-extended/`: a `<session>.jsonl` ledger of loads and flags, a `<session>.pending.json` of suspicions awaiting their grace window, and a `<session>.log` of events (`flag`, `suppress` with the path whose content matched, `worktree_switch`, `compact_drop`, `clear_reset`, `gc`, `error`). Logging is event-driven — steady-state tool calls write nothing — and capped at 1&nbsp;MB per session. Hooks never break a session: unexpected exceptions exit 0, with the traceback landing in the log rather than vanishing.

Abandoned state is garbage-collected after 30 days.

## Limitations

- **Bash extraction is best-effort.** Paths that never appear as command tokens aren't seen: shell variables (`cat "$DIR/x.py"` is invisible), `xargs`/`find` pipelines, and files named only in a command's *output*. `cd` is covered separately by `CwdChanged`.
- **`Grep` content matches.** A search rooted in one directory that returns hits deep elsewhere doesn't flag those directories until something actually touches them.
- **Managed-policy `CLAUDE.md` is untested.** The docs list `memory_type: "Managed"` and the plugin handles it like any other load, but the probe didn't write to the machine-wide policy path to confirm it.
- **Inlined files don't expand `@path` imports.** Claude Code only auto-resolves imports for files it loads natively, so the message tells Claude to follow them with the Read tool.

## Requirements

Python 3.10+ (pre-installed on macOS and most Linux distributions).

## License

MIT
