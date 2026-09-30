# claude-skills

A small collection of [Claude Code skills](https://code.claude.com/docs/en/skills).

## Skills

### afk

An orchestrator for AFK agent workers, one project per tmux session. A stdlib-only Python CLI (`afk`) finds the ticket frontier in a local `.scratch` spec or a GitHub spec issue's sub-issues (claiming GitHub tickets by assignment), starts each ticket in its own worktree, branch and split tmux window with an interactive `claude` worker (auto permission mode, per-session status hooks, and a deny list against force-push, pushing to base, `gh pr merge` and worktree removal), and tracks every worker's reported status. `afk watch` then drives each worker through implement → verify → prepr → pr → review by pasting phase prompts into its pane, shows a dashboard, marks tmux windows with each worker's state, and notifies you when a worker has a question, is blocked, stalls, or has a PR Ready.

Invoke with `/afk <init <spec> | frontier | start <ticket> | watch | status>`. The `afk` CLI must be on your PATH (see below).

### atomic-commits

A turn-based coding loop: plan a feature as a checklist of atomic commits, then build, stage, and land them one at a time with user review between each. The user is the gate — the agent stops only when there's something to review, and starts the next item the moment it's approved.

Invoke with `/atomic-commits <issue id or description of the work>`.

### brief-me

Builds a four-section dossier (Business Context, Technical Context, Possible Approaches, Risks) on a PR, issue, or open-ended task using parallel research subagents, then walks you through it as a user-paced tour. Useful for getting up to speed before reviewing or starting work on something.

Invoke with `/brief-me <PR number, ticket id, or task description>`.

### open-pr

Opens a pull request and drives it to a human-reviewable state: resolves merge conflicts, gets the checks green (or excuses a failure with named evidence), then waits for AI reviewers, triages each comment as valid / invalid / out-of-scope, and lands the valid fixes as atomic commits before resolving every thread. It never replies to human reviewers, never makes out-of-scope changes, and never merges — it just clears the automated gates. Hands off naturally from `atomic-commits`.

Invoke with `/open-pr [base branch]`.

## Installation

Copy the skill folders into your skills directory:

```bash
# Personal (all projects)
cp -r afk atomic-commits brief-me open-pr ~/.claude/skills/

# Or per-project
cp -r afk atomic-commits brief-me open-pr <project>/.claude/skills/
```

Each skill is a folder containing a `SKILL.md` (plus any supporting files) — see the [skills documentation](https://code.claude.com/docs/en/skills) for details.

### Putting `afk` on PATH

The `afk` skill drives a CLI that both you and its worker agents call. Symlink it onto your PATH (Python 3.11+, `tmux`, `git` required; `gh` for GitHub specs):

```bash
mkdir -p ~/.local/bin
ln -sf ~/.claude/skills/afk/afk.py ~/.local/bin/afk
afk   # prints usage
```

Its tests run with `python3 -m unittest` from the `afk/` folder.
