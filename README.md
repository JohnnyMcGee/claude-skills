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

### brief

The entry point of **process**, a pipeline that does the daily process chores (PR status and feedback, review requests, Linear inbox) and hands you one markdown brief. `/brief` runs `process run`, which takes every active project profile and runs each source's sweeper and then triage as headless `claude -p` phases. Each phase gets the profile's model and a tool allowlist that holds no send, review, RSVP or merge tool. The brief groups each project into **Needs approval** (drafts queued for you), **Done silently** (what Claude did without asking) and a collapsed **FYI**. It is written to `$XDG_STATE_HOME/process/brief.md`, and every brief of the day is also archived in `briefs/<date>.md`. Drafts wait in the queue for you to approve, edit or skip in `proc`; the executor sends what you approved, and the next brief shows how it went.

Invoke with `/brief [--delta] [--project <name>] [--fixtures <dir>]`. `--delta` sweeps from each source's watermark and shows only what changed; `--fixtures process/fixtures` is a dry run with no network. Needs the `process` CLI on PATH and at least one profile (see below).

### dossier

Builds a dossier: a prioritized, verified reading list that makes you an expert in a problem space before any planning starts. You describe the problem and the direction (or give a Linear issue id); it fixes the scope (general best practice, or that plus the internal system), fans out one research agent per area of interest, opens every link, and publishes a Claude Doc with a where-to-start path, a tiered source table per area, "easy to get wrong" gotchas, and the open questions the reading answers. It makes no decisions and never asks you to: the reading is what lets you plan.

Invoke with `/dossier <problem and direction, or issue id>`.

### execute

The executor of process: the one phase allowed to send. `process execute` (which `proc` runs when you quit it) first applies your decisions from `decisions.jsonl`: an approval, with your edit if you made one; a skip, which enters the handled-item ledger; promote-to-silent, which adds the action type to the profile's autonomy overrides; and a reason you typed, which goes into the profile's **How to sweep** section word for word, with a Changelog line. Then, for each profile with approved entries, it runs `/execute` headless. That submits approved reviews (never an approving one) and posts approved replies through `process execute review` and `reply`, which send exactly the approved text, and fixes your own PRs in a scratch worktree, pushing only to the PR's own branch and never with force. The executor holds no GitHub write tool of its own. It records each result (success with a link, or failure with the error) in `results.jsonl`, and the next brief shows them. `process run` also runs it after triage, so bot and nit comments on your PRs, and conflicts that only need the base merged in, are fixed and pushed without asking. Human review comments get a drafted reply and fix for your approval.

Run by `process execute [--project <name>]` as `/execute <profile> [--dry-run]`.

### open-pr

Opens a pull request and drives it to a human-reviewable state: resolves merge conflicts, gets the checks green (or excuses a failure with named evidence), then waits for AI reviewers, triages each comment as valid / invalid / out-of-scope, and lands the valid fixes as atomic commits before resolving every thread. It never replies to human reviewers, never makes out-of-scope changes, and never merges — it just clears the automated gates. Hands off naturally from `atomic-commits`.

Invoke with `/open-pr [base branch]`, or let an agent reach it by name (AFK workers use it in their pr phase).

### sweep-github

The GitHub sweeper of process. For one profile it reads my open PRs (mergeability, checks, review state, new comments) and the PRs requesting my review, maps each ask to an **item**, and stores it with `process emit`, which advances the profile's GitHub watermark. Read-only.

Run by `process run` as `/sweep-github <profile> [--delta] [--fixtures <dir>]`.

### sweep-linear

The Linear sweeper of process. For one profile it reads unread notifications, mentions, and changes to scope, blockers or due dates on tickets I own or that block or are blocked by mine, and stores them as items. Linear's mirrored GitHub review requests carry the PR URL, so triage merges them with the GitHub item. Read-only: triage is the one that marks notifications read.

Run by `process run` as `/sweep-linear <profile> [--delta] [--fixtures <dir>]`.

### triage

The triage step of process. `process triage plan` applies every rule deterministically: it dedupes the same ask across sources, decides what needs me, sets autonomy and urgency, and never proposes a merge, an approving review or a prod action. The first run also clears Linear notifications older than 7 days. Claude then writes the drafts and marks processed Linear notifications read, and `process triage commit` validates everything against the shared schema before it writes the queue and the handled-item ledger. An item that changes after it was handled comes back as a new version.

Run by `process run` as `/triage <profile> [--dry-run]`.

## Installation

Copy the skill folders into your skills directory:

```bash
# Personal (all projects)
cp -r afk afk-setup atomic-commits brief dossier execute open-pr process sweep-github sweep-linear triage ~/.claude/skills/

# Or per-project
cp -r afk afk-setup atomic-commits brief dossier execute open-pr process sweep-github sweep-linear triage <project>/.claude/skills/
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

### Setting up process

`brief`, `sweep-github`, `sweep-linear`, `triage` and `execute` drive the `process` CLI in the `process/` folder (Python 3.11+, stdlib only; `gh` and `claude` on PATH). It is not a skill itself, but it is copied alongside them. Put it on PATH, then give it a profile per project:

```bash
ln -sf ~/.claude/skills/process/process.py ~/.local/bin/process
mkdir -p ~/.config/process/profiles
cp ~/.claude/skills/process/profiles/acme.md ~/.config/process/profiles/<project>.md   # then edit it
process profiles   # validates every profile and refuses one that grants a send tool
```

A profile is markdown with TOML front matter: the project's sources, repo, Linear team, Google account, urgency rules, autonomy overrides, and a model and tool allowlist per phase. Its body is a **How to sweep** section and a **Changelog**. `process/profiles/acme.md` is an example. Keep your real profiles in `~/.config/process/profiles/`, not in this repo. The contract between phases (item, queue entry, decision, result, profile) is `process/schema.json`. State lives in `$XDG_STATE_HOME/process/`: `queue.jsonl`, `decisions.jsonl` (appended by `proc`), `results.jsonl` (appended by the executor), the brief, and per profile its items, watermarks and ledger. Only a profile's `[phases.execute]` may grant send tools, and no phase may grant a merge, an approval, a delete or a raw push. Tests run with `python3 -m unittest` from the `process/` folder.
