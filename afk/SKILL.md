---
name: afk
description: Orchestrate AFK agent workers for one project in this tmux session — find unblocked tickets, start workers in their own worktree and tmux window, and track their status.
disable-model-invocation: true
argument-hint: <init <spec> | frontier | start <ticket> [--type <type>] | watch | status | next>
---

# AFK

You drive the `afk` CLI; it does all the mechanical work. Run it with Bash and relay its output. Add judgment only where a step below asks for it.

One project = one tmux session. `afk` resolves the project from the tmux session it runs in, so run every command from inside that session.

## Commands

- **`afk init <spec>`** — create the project from a local spec directory (`.scratch/<slug>/` or its `spec.md`), or from a GitHub spec issue URL whose sub-issues are the tickets (run it from inside that repo's clone), and bind it to the current tmux session. Run once per project.
- **`afk frontier`** — list open, unclaimed tickets whose blockers are all resolved/done/closed. On GitHub a ticket is claimed when it has any assignee, blockers are its native blocked-by dependencies (else its `## Blocked by` section), and a `Repo: owner/name` line shows as `(owner/name)`; without one the repo is unresolved.
- **`afk start <ticket> [--type <type>]`** — claim the ticket (on GitHub, assign it to you), create a worktree and branch `afk/<project>-<ticket>` from the repo's base branch, copy its gitignored files in and run its bootstrap commands, allocate a slot (`AFK_SLOT`, `AFK_PORT_BASE`), open a tmux window `<ticket>-<abbrev>` split agent-left / shell-right, and launch an interactive `claude` worker in auto permission mode with per-session hooks and a deny list (no force-push, no push to base, no `gh pr merge`, no worktree removal). The ticket's task type picks the model, effort, skill and prompt template. All of this comes from the repo's `docs/agents/afk.md`; see [repo-config.md](repo-config.md).
- **`afk watch`** — the watcher: run it in the orchestrator window's right pane. Every couple of seconds it advances workers through their phases, redraws the dashboard (ticket, phase, state, time in phase, PR), marks each worker's tmux window with its state, and notifies (bell, tmux message, `notify-send`) when a worker needs the human. It needs no LLM. `afk tick` runs one step of it.
- **`afk status`** — every worker's ticket, phase, state and last message.

## Handling the argument

- `init <spec>`, `frontier`, `start <ticket> [--type <type>]`, `status`: run `afk $ARGUMENTS` and show the output.
- `watch`: it runs forever, so don't run it yourself. Tell the user to run `afk watch` in the orchestrator window's right pane.
- `next`: run `afk status` and triage every `stuck` worker: name its window, say which limit tripped (its message), and read the tail of its agent pane (`tmux capture-pane -p -t <pane>`, with `pane` from `${XDG_STATE_HOME:-~/.local/state}/afk/<project>/workers/<ticket>/status.json`; `-t <window>` would capture whichever pane is active) to judge why. Propose one of: nudge it in its pane, raise the limit in the project config, or leave it for the user to take over. Act only on what the user approves.
- No argument: run `afk status`, then `afk frontier`, and ask which ticket to start.

Never start a ticket the user has not named or approved.

## Phases

The watcher drives each worker through `implement → verify → prepr → pr → review`, pasting the next phase's prompt (`phase-<name>.md` in this folder) into the worker's pane. It sends a prompt only after the worker has reported `done` *and* its session has stopped, so it never types into a busy worker. If the repo sets `verify_concurrency`, a worker due to verify while the slots are full waits with state `queued` and is started when one frees. Reaching `review` means the PR is Ready; the review prompt gives standing instructions for handling feedback the user types into the pane.

## Worker protocol

Workers end every phase with `afk report <done|blocked|question> "<message>"`. That, plus the hooks, is what `afk status` shows.

- `question` or `blocked`: the watcher notifies and does not advance. Tell the user which window needs them — do not answer on the worker's behalf.
- A worker that stops without reporting since its last phase prompt is marked `attention` (window suffix `!`). It has stalled silently and needs the user.
- A worker due its next phase whose pane no longer runs `claude` (it exited, or the pane was killed) is also marked `attention`; the watcher never pastes a prompt into a bare shell.

## Limits

The watcher contains runaway workers. Defaults, overridable in the project's `config.toml` under `[limits]`:

```toml
[limits]
max_workers = 3     # `afk start` refuses beyond this; workers in review don't count
phase_minutes = 90  # wall-clock in one phase while working
idle_minutes = 20   # working, but its transcript hasn't changed
fix_loops = 3       # review rounds (feedback → fix → done) before stopping to look
```

A tripped limit interrupts the worker (Escape in its pane; not for `fix_loops`, where it has already stopped), marks it `stuck` with the reason (window suffix `!`) and notifies. Nothing is killed: the session stays open for the user or `/afk next` triage. A limit trips at most once per phase, so a stuck worker that is nudged and later reports `done` moves on as usual.

## Setup

`afk` must be on PATH for workers and the user — see this repo's README.
