---
name: afk
description: Orchestrate AFK agent workers for one project in this tmux session — find unblocked tickets, start workers in their own worktree and tmux window, and track their status.
disable-model-invocation: true
argument-hint: <init <spec> | frontier | start <ticket> | status>
---

# AFK

You drive the `afk` CLI; it does all the mechanical work. Run it with Bash and relay its output. Add judgment only where a step below asks for it.

One project = one tmux session. `afk` resolves the project from the tmux session it runs in, so run every command from inside that session.

## Commands

- **`afk init <spec>`** — create the project from a local spec directory (`.scratch/<slug>/` or its `spec.md`) and bind it to the current tmux session. Run once per project.
- **`afk frontier`** — list open, unclaimed tickets whose blockers are all resolved/done/closed.
- **`afk start <ticket>`** — create a worktree and branch `afk/<project>-<ticket>`, open a tmux window `<ticket>-<abbrev>` split agent-left / shell-right, and launch an interactive `claude` worker in auto permission mode with per-session hooks and a deny list (no force-push, no push to base, no `gh pr merge`, no worktree removal).
- **`afk status`** — every worker's ticket, phase, state and last message.

## Handling the argument

- `init <spec>`, `frontier`, `start <ticket>`, `status`: run `afk $ARGUMENTS` and show the output.
- No argument: run `afk status`, then `afk frontier`, and ask which ticket to start.

Never start a ticket the user has not named or approved.

## Worker protocol

Workers end every stretch of work with `afk report <done|blocked|question> "<message>"`. That, plus the hooks, is what `afk status` shows. When a worker reports `question` or `blocked`, tell the user which window needs them — do not answer on the worker's behalf.

## Setup

`afk` must be on PATH for workers and the user — see this repo's README.
