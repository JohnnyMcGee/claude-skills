# claude-skills

A small collection of [Claude Code skills](https://code.claude.com/docs/en/skills).

## Skills

### afk

An orchestrator for AFK agent workers: you choose the work, agents build it in parallel, and you merge. One project per tmux session. Tickets come from a local `.scratch` spec or a GitHub spec issue's sub-issues. `/afk next` proposes a batch of unblocked tickets that won't collide with each other or with running workers (Gate 1), and offers to split any ticket that spans repos. Each ticket you approve gets its own worktree, branch, port slot and tmux window with an interactive `claude` worker. The repo's `docs/agents/afk.md` sets these up, and a deny list keeps the worker from force-pushing, pushing to base or merging.

`afk watch` then drives each worker through implement → verify → prepr → pr → review. It shows a dashboard, colours windows by state, and notifies you when a worker has a question, is blocked, stalls, trips a runaway limit, or has a PR Ready. Merging is up to you. Once a PR merges, the watcher removes the worker's worktree, branch and window, but only if the shell pane is idle and the worktree is clean. It then prompts you for the next batch. A `hitl` ticket gets the same setup, but its worker runs in guide mode and helps you do the work yourself.

Invoke with `/afk <init <spec> | next | frontier | start <ticket> | watch | status>`. The `afk` CLI must be on your PATH (see below).

### afk-setup

Onboards a repo for `afk`, once per repo. It inspects the repo and interviews you to write `docs/agents/afk.md` (base branch, gitignored files to copy, bootstrap, dev-server port and database isolation, verification recipes, task types), creates a `/pre-pr` skill mirroring CI if the repo has none, then proves the config: a scratch worktree, bootstrap, and the dev server started and stopped on a slot port, reporting exactly which step failed. On a new machine it also puts `afk` on PATH, adds a tmux snippet that colours worker windows by state, and checks for `notify-send`, skipping whatever is already done.

Invoke with `/afk-setup` from the repo.

### atomic-commits

A turn-based coding loop: plan a feature as a checklist of atomic commits, then build, stage, and land them one at a time with user review between each. The user is the gate — the agent stops only when there's something to review, and starts the next item the moment it's approved.

Invoke with `/atomic-commits <issue id or description of the work>`.

### brief-me

Builds a four-section dossier (Business Context, Technical Context, Possible Approaches, Risks) on a PR, issue, or open-ended task using parallel research subagents, then walks you through it as a user-paced tour. Useful for getting up to speed before reviewing or starting work on something.

Invoke with `/brief-me <PR number, ticket id, or task description>`.

### open-pr

Opens a pull request and drives it to a human-reviewable state: resolves merge conflicts, gets the checks green (or excuses a failure with named evidence), then waits for AI reviewers, triages each comment as valid / invalid / out-of-scope, and lands the valid fixes as atomic commits before resolving every thread. It never replies to human reviewers, never makes out-of-scope changes, and never merges — it just clears the automated gates. Hands off naturally from `atomic-commits`.

Invoke with `/open-pr [base branch]`, or let an agent reach it by name (AFK workers use it in their pr phase).

## Installation

Copy the skill folders into your skills directory:

```bash
# Personal (all projects)
cp -r afk afk-setup atomic-commits brief-me open-pr ~/.claude/skills/

# Or per-project
cp -r afk afk-setup atomic-commits brief-me open-pr <project>/.claude/skills/
```

Each skill is a folder containing a `SKILL.md` (plus any supporting files) — see the [skills documentation](https://code.claude.com/docs/en/skills) for details.

### Putting `afk` on PATH

The `afk` skill drives a CLI that both you and its worker agents call. `/afk-setup` symlinks it onto your PATH on first use; to do it by hand (Python 3.11+, `tmux`, `git` required; `gh` for GitHub specs):

```bash
mkdir -p ~/.local/bin
ln -sf ~/.claude/skills/afk/afk.py ~/.local/bin/afk
afk   # prints usage
```

Its tests run with `python3 -m unittest` from the `afk/` folder. [`afk/e2e-checklist.md`](afk/e2e-checklist.md) is a manual end-to-end run of the whole loop with real tmux and `claude`.
