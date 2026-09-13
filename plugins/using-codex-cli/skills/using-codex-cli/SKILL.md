---
name: using-codex-cli
description: Use when driving the OpenAI Codex CLI (`codex`) directly from Bash, scripts, or agents — headless runs with codex exec, resuming sessions, non-interactive code reviews, JSONL event output, sandbox/approval flags — or when Codex CLI flags need verification before use. Not for the spencer-codex plugin's /codex:* commands.
---

# Using the Codex CLI

Headless Codex = `codex exec`. Everything here was verified by execution against codex-cli 0.149.1 (2026-08); on a newer major version, re-check surprising behavior with `--help` before relying on it.

**Precedence:** if the `codex` (spencer-codex) plugin is installed and the task is a review or a delegated fix/investigation, use its `/codex:*` commands instead (see the `codex:using-codex` skill) — they add job tracking and background handling. Use the raw CLI for scripting, automation, session surgery, and machines without the plugin.

## Rules

- Never run bare `codex "prompt"` from an agent. It is the interactive TUI; without a TTY it exits 1 with `Error: stdin is not a terminal`.
- Always pass the prompt as an argument. With no prompt argument, `exec` reads instructions from stdin (blocks while stdin is open); with a prompt AND piped stdin, stdin is appended to the prompt as a `<stdin>` block. In scripts, add `</dev/null` unless you want that.
- Outside a git repo or trusted directory, every run — including `exec resume` — needs `--skip-git-repo-check`, or it exits 1 with `Not inside a trusted directory...`. The flag is not remembered from the session being resumed.

## Quick reference

| Task | Command |
|---|---|
| Ask headlessly, read-only | `codex exec -s read-only "question"` |
| Capture only the final answer | add `-o /path/answer.md` (also printed to stdout when piped) |
| Headless run that may edit | `codex exec -s workspace-write "task"` |
| Continue latest session | `codex exec resume --last "follow-up"` |
| Continue a specific session | `codex exec resume <thread_id> "follow-up"` |
| Branch off a session | `codex exec fork <thread_id> "prompt"` |
| Review uncommitted changes | `codex exec review --uncommitted -o /path/review.md` |
| Review branch vs base / one commit | `codex exec review --base main` / `--commit <sha>` |
| Machine-readable events | add `--json` (JSONL on stdout; includes `thread_id`) |
| Structured final answer | `--output-schema schema.json` |
| Preflight | `codex login status`, `codex doctor`, `codex --version` |

## Output contract

When stdout is piped, stdout carries **only the final agent message**; all progress, transcript, and token counts go to stderr. Do not redirect stdout to /dev/null expecting noise — you'd discard the answer. `--json` replaces this with JSONL events (see [headless-details.md](references/headless-details.md) for the verified event shapes and a sample review finding).

## Sandbox and approvals

- `-s read-only | workspace-write | danger-full-access`. Default comes from `~/.codex/config.toml`, so pass `-s` explicitly in scripts.
- `exec` never prompts a human: it has no `-a/--ask-for-approval` flag and hardcodes approval policy to `never` — blocked actions fail back to the model. `-a` exists only on the interactive TUI (values: `on-request`, `never`).
- Extra writable roots: `--add-dir <dir>`. Working root: `-C <dir>`.
- `--dangerously-bypass-approvals-and-sandbox` is typically the ideal default to avoid any conflicts. Codex models are good at instruction following, simply instruct them what to do and not to and what is in scope and not in scope, this flag is also aliased to `--yolo`

## Gotchas (each cost a real failed run or a wrong baseline belief)

| Trap | Reality |
|---|---|
| `codex exec review --uncommitted "focus on X"` | Scope flags (`--uncommitted`, `--base`, `--commit`) are mutually exclusive with a custom prompt — clap errors. With a custom prompt, scope is model-determined (can include HEAD commits and untracked files): state the intended scope in the prompt. |
| `codex review` in scripts | Works, but only `codex exec review` has `-o`/`--json`. Prefer the exec form. |
| `exec resume --last` from another directory | `--last` is cwd-filtered; it can silently resume a different session. `--all` disables the filter. |
| `--ephemeral` then resume | Ephemeral runs persist no session — there is nothing to resume. |
| `--full-auto`, `-q/--quiet`, `-a on-failure` | Removed. Training-data flags; check `--help` before using any flag not listed here. |

For session management (`queue`, `apply`, `cloud`), MCP, config overrides (`-c`, `-p`, `--enable`), and the rest of the surface: [headless-details.md](references/headless-details.md).
