---
name: afk
description: Orchestrate AFK agent workers for one project in this tmux session — find unblocked tickets, start workers in their own worktree and tmux window, and track their status.
disable-model-invocation: true
argument-hint: <init <spec> | next | frontier | start <ticket> [--type <type>] | watch | status>
---

# AFK

You drive the `afk` CLI; it does all the mechanical work. Run it with Bash and relay its output. Add judgment only where a step below asks for it.

One project = one tmux session. `afk` resolves the project from the tmux session it runs in, so run every command from inside that session.

## Commands

- **`afk init <spec>`** — create the project from a local spec directory (`.scratch/<slug>/` or its `spec.md`), or from a GitHub spec issue URL whose sub-issues are the tickets (run it from inside that repo's clone), and bind it to the current tmux session. Run once per project.
- **`afk frontier`** — list open, unclaimed tickets whose blockers are all resolved/done/closed. On GitHub a ticket is claimed when it has any assignee, blockers are its native blocked-by dependencies (else its `## Blocked by` section), and a `Repo: owner/name` line shows as `(owner/name)`; without one the repo is unresolved.
- **`afk next`** — JSON for Gate 1: free `capacity` (the project's `[limits] max_workers`, default 3, minus running workers), the running `workers`, and the unblocked, unstarted `tickets`, each with its `repo`, local `clone` (`null` when the project has none), and the `type`, `agent`, `model` and `effort` it would launch with.
- **`afk split <ticket>`** — replace a ticket spanning repos with one ticket per repo. Stdin is a JSON list of `{"repo", "title", "body"}`. The parts inherit its blockers, its dependents wait for every part, and it is closed.
- **`afk start <ticket> [--type <type>] [--agent <agent>] [--model <model>] [--effort <effort>] [--repo <owner/name>]`** — each option corrects that field of `afk next`'s proposal, and the worker's status records what it launched with. `--repo` picks the clone from the project's `[repos]` table. Start fails once `max_workers` workers run; otherwise it claims the ticket (on GitHub, assigns it to you), creates a worktree and branch `afk/<project>-<ticket>` from the repo's base branch, copies its gitignored files in and runs its bootstrap commands, allocates a slot (`AFK_SLOT`, `AFK_PORT_BASE`), opens a tmux window `<ticket>-<abbrev>` split agent-left / shell-right, and launches an interactive `claude` worker in auto permission mode with per-session hooks and a deny list (no force-push, no push to base, no `gh pr merge`, no worktree removal). The ticket's task type picks the model, effort, skill and prompt template. All of this comes from the ticket's repo's `docs/agents/afk.md`; see [repo-config.md](repo-config.md).
- **`afk watch`** — the watcher: run it in the orchestrator window's right pane. Every couple of seconds it advances workers through their phases, redraws the dashboard (ticket, phase, state, time in phase, PR), marks each worker's tmux window with its state, and notifies (bell, tmux message, `notify-send`) when a worker needs the human. When tickets are unblocked and a worker slot is free, it shows "N tickets unblocked — run /afk next" and notifies once per newly unblocked ticket. It needs no LLM. `afk tick` runs one step of it.
- **`afk status`** — every worker's ticket, phase, state and last message.

## Handling the argument

- `next`: Gate 1 — follow [Gate 1](#gate-1-afk-next) below.
- `init <spec>`, `frontier`, `start <ticket> …`, `status`: run `afk $ARGUMENTS` and show the output.
- `watch`: it runs forever, so don't run it yourself. Tell the user to run `afk watch` in the orchestrator window's right pane.
- No argument: run `afk status`, then `afk frontier`, and ask which ticket to start.

Start only tickets the user has named or approved.

## Gate 1: `/afk next`

The user's gate for what work starts. Propose; the user decides.

1. **Gather.** Run `afk status` and `afk next`. If `capacity` is 0 or `tickets` is empty, say which and stop.
2. **Read every candidate.** Open each ticket's `path` (a file, or an issue URL: `gh issue view <url>`). Done when you know, for each, which repo or repos its changes land in and which areas of code it touches.
3. **Resolve repos.** A ticket's `repo` comes from its `Repo:` line. When it is `null` in a project with several clones, infer it from the ticket and mark it inferred. When its `clone` is `null`, the project has no clone of that repo: tell the user to add it under `[repos]` in the project's `config.toml`.
4. **Flag multi-repo tickets.** Each ticket must land in exactly one repo. For each one spanning repos, draft its parts — one per repo, each with a title and a self-contained body carrying the original's acceptance criteria for that repo — and offer the split. On approval, pipe the parts as JSON to `afk split <ticket>`, then re-run `afk next`, since the parts are now candidates.
5. **Choose the batch.** Up to `capacity` tickets that can be built in parallel. Weigh conflict risk: two tickets editing the same files, schema, migrations or shared interfaces collide, with each other and with running `workers`. Prefer the batch with the least overlap; defer the rest.
6. **Present.** A table: ticket, title, repo (marked when inferred), type, agent, model, effort. Below it, the conflict-risk reasoning for the batch and why each deferred ticket waits. Ask the user to approve or correct any field.
7. **Start.** For each approved ticket run `afk start <ticket>` with an option for each field the user corrected or you inferred (`--type`, `--agent`, `--model`, `--effort`, `--repo`); the worker's status persists them. Done when every approved ticket has started, or a start has failed: report the failure and stop.

## Phases

The watcher drives each worker through `implement → verify → prepr → pr → review`, pasting the next phase's prompt (`phase-<name>.md` in this folder) into the worker's pane. It sends a prompt only after the worker has reported `done` *and* its session has stopped, so it never types into a busy worker. If the repo sets `verify_concurrency`, a worker due to verify while the slots are full waits with state `queued` and is started when one frees. Reaching `review` means the PR is Ready; the review prompt gives standing instructions for handling feedback the user types into the pane.

## Worker protocol

Workers end every phase with `afk report <done|blocked|question> "<message>"`. That, plus the hooks, is what `afk status` shows.

- `question` or `blocked`: the watcher notifies and does not advance. Tell the user which window needs them — do not answer on the worker's behalf.
- A worker that stops without reporting since its last phase prompt is marked `attention` (window suffix `!`). It has stalled silently and needs the user.
- A worker due its next phase whose pane no longer runs `claude` (it exited, or the pane was killed) is also marked `attention`; the watcher never pastes a prompt into a bare shell.

## Setup

`afk` must be on PATH for workers and the user — see this repo's README.
