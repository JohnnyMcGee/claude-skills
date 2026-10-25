---
name: afk
description: Orchestrate AFK agent workers for one project in this tmux session — find unblocked tickets, start workers in their own worktree and tmux window, and track their status.
disable-model-invocation: true
argument-hint: <init [<spec>] [--land <pr|local>] [--target <branch>] | frontier | start <ticket> [--type <type>] | watch | status | view | next>
---

# AFK

You drive the `afk` CLI; it does all the mechanical work. Run it with Bash and relay its output. Add judgment only where a step below asks for it.

One project = one tmux session. `afk` resolves the project from the tmux session it runs in, so run every command from inside that session.

## Commands

- **`afk init <spec> [--land <pr|local>] [--target <branch>]`** — create the project from a local spec directory (`.scratch/<slug>/` or its `spec.md`), or from a GitHub spec issue URL whose sub-issues are the tickets (run it from inside that repo's clone), and bind it to the current tmux session. It maps every repo the tickets live in or name on a `Repo:` line to a local clone, found by its git remotes among the spec clone's submodules, its subdirectories and its siblings, records them under `[repos]`, and names any repo it found no clone of. Re-run it (with no argument, from the project's session) to refresh the project after adding clones or tickets from new repos: it keeps `[landing]`, `[limits]`, `[overrides]` and hand-added `[repos]` entries whose directory still exists. `--land` sets the project's [landing mode](#landing-mode); an unknown mode fails before anything is written.
- **`afk frontier`** — list open, unclaimed tickets whose blockers are all resolved/done/closed. On GitHub a ticket is claimed when it has any assignee, blockers are its native blocked-by dependencies (else its `## Blocked by` section), and a `Repo: owner/name` line shows as `(owner/name)`; without one the repo is unresolved. A sub-issue in another of the spec owner's repos is named `<repo>#<number>` (e.g. `web#129`), defaults to its own repo, and is claimed and checked there; a sub-issue under another owner is refused. In a `## Blocked by` section, `#<number>` means the issue's own repo and `owner/name#<number>` any other.
- **`afk next`** — JSON for Gate 1: free `capacity` (the project's `[limits] max_workers`, default 3, minus workers not yet in review, not counting hitl workers), the running `workers`, and the unblocked, unstarted `tickets`, each with its `repo`, local `clone` (`null` when the project has none), and the `type`, `agent`, `model` and `effort` it would launch with.
- **`afk split <ticket>`** — replace a ticket spanning repos with one ticket per repo. Stdin is a JSON list of `{"repo", "title", "body"}`. The parts inherit its blockers, its dependents wait for every part, and it is closed.
- **`afk start <ticket> [--type <type>] [--agent <agent>] [--model <model>] [--effort <effort>] [--repo <owner/name>]`** — each option corrects that field of `afk next`'s proposal, and the worker's status records what it launched with, including its landing mode and target, so changing the project's `[landing]` never strands a running worker. `--repo` picks the clone from the project's `[repos]` table. Start fails once `max_workers` workers are active (workers in review and hitl workers don't count, and a hitl start is never refused); otherwise it claims the ticket (on GitHub, assigns it to you), creates a worktree and branch `afk/<project>-<ticket>` from the repo's base branch (in local mode, from the target branch), copies its gitignored files in and runs its bootstrap commands, allocates a slot (`AFK_SLOT`, `AFK_PORT_BASE`), opens a tmux window `<ticket>-<abbrev>` split agent-left / shell-right, and launches an interactive `claude` worker in auto permission mode with per-session hooks and a deny list (no force-push, no push to base, no `gh pr merge`, no worktree removal; in local mode also no pushing, `git update-ref` on, or checking out or switching to the target, which only `afk land` moves). The ticket's task type picks the model, effort, skill and prompt template; a `hitl` ticket gets a guide-mode prompt instead of implement (see [HITL tickets](#hitl-tickets)). All of this comes from the ticket's repo's `docs/agents/afk.md`; see [repo-config.md](repo-config.md).
- **`afk watch`** — the watcher: run it in the orchestrator window's right pane. Every couple of seconds it advances workers through their phases, redraws the dashboard (ticket, phase, state, time in phase, PR), marks each worker's tmux window with its state, and notifies (bell, tmux message, `notify-send`) when a worker needs the human. When tickets are unblocked and a worker slot is free, it shows "N tickets unblocked — run /afk next" and notifies once per newly unblocked ticket. In a `local`-mode project landing on an integration branch, once every ticket is done or closed and no worker is left, it shows "all tickets landed on `<target>`; open a PR to `<base>` when ready" and notifies once; a ticket added later reopens the project, to notify again when it too has landed. It needs no LLM. `afk tick` runs one step of it.
- **`afk land "<message>"`** — run by a local-mode worker, from its worktree, once the human approves its branch: see [Landing](#landing).
- **`afk status`** — every worker's ticket, phase, state and last message, then the same "all tickets landed" line the watcher shows, while it holds.
- **`afk view`** — the current project's config (`config.toml`): its spec, tracker, repo, session, `[landing]`, `[repos]`, `[limits]` and `[overrides]`.

## Handling the argument

- `next`: follow [`/afk next`](#afk-next) below.
- `init [<spec>] …`, `frontier`, `start <ticket> …`, `status`, `view`: run `afk $ARGUMENTS` and show the output.
- `watch`: it runs forever, so don't run it yourself. Tell the user to run `afk watch` in the orchestrator window's right pane.
- No argument: run `afk status`, then `afk frontier`, and ask which ticket to start.

Start only tickets the user has named or approved.

## `/afk next`

The judgment points: stuck workers, then Gate 1, the user's gate for what work starts. Propose; the user decides.

1. **Triage stuck workers.** Run `afk status`. For every `stuck` worker: name its window, say which limit tripped (its message), and read the tail of its agent pane (`tmux capture-pane -p -t <pane>`, with `pane` from `${XDG_STATE_HOME:-~/.local/state}/afk/<project>/workers/<ticket>/status.json`; `-t <window>` would capture whichever pane is active) to judge why. Propose one of: nudge it in its pane, raise the limit in the project config, or leave it for the user to take over. Act only on what the user approves.
2. **Gather.** Run `afk next`. If `capacity` is 0 or `tickets` is empty, say which and stop.
3. **Read every candidate.** Open each ticket's `path` (a file, or an issue URL: `gh issue view <url>`). Done when you know, for each, which repo or repos its changes land in and which areas of code it touches.
4. **Resolve repos.** A ticket's `repo` comes from its `Repo:` line. When it is `null` in a project with several clones, infer it from the ticket and mark it inferred. When its `clone` is `null`, the project has no clone of that repo: tell the user to clone it next to the project's repo (or as a submodule) and run `afk init` again, or to add it under `[repos]` in the project's `config.toml`.
5. **Flag multi-repo tickets.** Each ticket must land in exactly one repo. For each one spanning repos, draft its parts — one per repo, each with a title and a self-contained body carrying the original's acceptance criteria for that repo — and offer the split. On approval, pipe the parts as JSON to `afk split <ticket>`, then re-run `afk next`, since the parts are now candidates.
6. **Choose the batch.** Up to `capacity` tickets that can be built in parallel. Weigh conflict risk: two tickets editing the same files, schema, migrations or shared interfaces collide, with each other and with running `workers`. Prefer the batch with the least overlap; defer the rest.
7. **Present.** A table: ticket, title, repo (marked when inferred), type, agent, model, effort. Below it, the conflict-risk reasoning for the batch and why each deferred ticket waits. Ask the user to approve or correct any field.
8. **Start.** For each approved ticket run `afk start <ticket>` with an option for each field the user corrected or you inferred (`--type`, `--agent`, `--model`, `--effort`, `--repo`); the worker's status persists them. Done when every approved ticket has started, or a start has failed: report the failure and stop.

## Landing mode

A project's landing mode says how its workers' work gets into the target branch. It is set by `afk init` and stored in the project's `config.toml`:

```toml
[landing]
mode = "local"         # "pr" (the default, when there is no [landing]) or "local"
target = "widgets-v2"  # optional; without it, each repo's work lands on its own base
```

- **`pr`**: every ticket opens a PR against the repo's base, which the human merges.
- **`local`**: workers never open a PR or push their branch. Their worktrees branch from the target, and prepr checks for merge conflicts against the target, a local branch, so a ticket starts from its blockers' landed work. The target is the repo's `base`, or an integration branch given with `--target`: `afk init` creates an integration branch that doesn't exist yet from each clone's base (the project's own clone and every clone in `[repos]`), under the same name, and leaves one that exists as it is.

`--target` needs local mode, given with `--land local` or already the project's. `afk init --land pr` switches a project back, dropping `[landing]`.

## Phases

The watcher drives each worker through `implement → verify → prepr → pr → review` (in local mode `implement → verify → prepr → critique → review`: there is no `pr` phase), pasting the next phase's prompt (`phase-<name>.md` in this folder, or `phase-<name>-local.md` where local mode has its own) into the worker's pane. Prompts get `{{ticket}}`, `{{ticket_path}}`, `{{base}}`, `{{target}}` and `{{reviewers}}` filled in. It sends a prompt only after the worker has reported `done` *and* its session has stopped, so it never types into a busy worker. If the repo sets `verify_concurrency`, a worker due to verify while the slots are full waits with state `queued` and is started when one frees. Reaching `review` means the PR is Ready (in local mode, that the branch is ready); the review prompt gives standing instructions for handling feedback the user types into the pane.

**Critique** (local mode only) stands in for the bot reviewers a PR would get. The worker runs each of the repo's `reviewers` commands (see [repo-config.md](repo-config.md#reviewers)) over `git diff <target>...HEAD`, skipping and naming any whose binary is missing, or a code-review subagent when none runs. It sorts the findings as `/open-pr` sorts bot comments: valid, in-scope ones fixed and committed, the rest rejected with a one-line reason. Each prompt is one round. A round that fixed anything ends with `afk report done "round: …"`, and the watcher sends the critique prompt again so the fixes are critiqued too; a round with nothing to fix ends with a plain `done`, counting the rejected findings, and moves the worker to `review`. More rounds than `critique_rounds` allows make it `stuck` (see [Limits](#limits)). In local mode the review prompt's standing instructions also critique the diff again after addressing the user's feedback, and land the branch with `afk land` once the user approves it: see [Landing](#landing).

## Landing

In local mode the human approves a worker's branch in its pane, and the worker lands it. Following the review prompt's standing instructions, a worker given feedback fixes it, re-runs the pre-PR skill, merges the target and critiques the changed diff, then reports `done`. Given approval ("approve", "land it", "lgtm"), it writes a conventional commit message (`type(scope): summary`, a body, a `Ticket: <id>` trailer) and runs `afk land "<message>"`. Unsure which it was given, it asks.

**Approval.** The worker's prompt hook records when the human last typed into its pane (`last_human_prompt_at` in its status). Prompts the watcher pastes don't count: afk's own prompts begin `AFK phase:` or `AFK guide mode`, after the task type's skill if any, or inside the `<pasted_content>` tag Claude Code wraps a paste in. Nor do the `<task-notification>` prompts Claude Code submits when a background task finishes. A prompt typed while the worker is busy is queued, but counts from when it was typed. Approval goes stale once the branch has a later commit of its own, so feedback followed by fixes needs approving again. A merge of the target after approval keeps it if git made the merge unaided, with no conflicts and nothing else changed; one whose conflicts the worker resolved needs approving again, as does a merge of any other branch.

**`afk land "<message>"`** refuses, changing nothing, when:

- the worker lands by PR, or the message is empty;
- the branch has no commits of its own, or none approved since its last one;
- the target isn't an ancestor of the branch: "target moved; merge it, re-run pre-PR, land again";
- the target is checked out in a worktree with uncommitted changes to tracked files (often `main` in the clone), naming its path.

Otherwise it builds one commit with the message and the branch's tree on top of the target, and moves the target to it by compare-and-swap (`git update-ref <ref> <new> <old>`). Of two workers landing at once, one wins and the other refuses with "target moved", leaving nothing changed. A clean worktree with the target checked out is fast-forwarded to it, one land in a repo at a time. An integration branch is then pushed (fast-forward only); base never is. A failed push, or a checkout git won't update (say an untracked file is in the way), notifies and doesn't undo the land. The worker's status records `landed_sha` as soon as the target moves, so the land is never repeated, and `ended = "landed"` once it has finished, so the watcher never cleans up a worker mid-land.

## HITL tickets

A ticket of the built-in `hitl` type is the user's to work on. `afk start` sets it up exactly as it would any other ticket: worktree, bootstrap, slot and window. But its worker is launched in guide mode (`phase-hitl.md`), where it helps the user rather than doing the work. The watcher never drives it through phases, never prompts it, and doesn't hold it to phase or idle limits. It shows as phase `hitl`, state `yours`. It doesn't count toward `max_workers`, and starting one is never refused for being over that limit. The watcher finds its PR by branch (`afk/<project>-<ticket>`) once the user opens one. Cleanup works as below: after a merge, after the PR is closed, or, when the work needs no PR, after the ticket is closed.

## Merge and cleanup

The watcher looks up each `pr`, `review` and `hitl` worker's PR by its branch (`afk/<project>-<ticket>`) with `gh pr view` about once a minute, so a PR is found whoever opened it and whether or not its URL was reported. A local-mode worker opens none, so it is never looked up, unless it's a hitl worker. A worker stopped in `pr` without reporting `done` moves on to `review` only once its open PR carries the `Ready for Review` label, which `/open-pr` adds when the automated gates are clear. An open PR alone isn't enough, because `/open-pr` stops between turns while it waits on checks and reviewers. If you opened or finished a stuck worker's PR yourself, add the label to hand it over. A `review` worker whose branch has no PR needs attention until one is found. It never merges a PR or requests reviewers: merging is the user's call. At the same cadence it reads the worker's ticket from the tracker. The worker's work has ended once it has landed, its PR is merged, its PR is closed unmerged, or its ticket is closed. Then the watcher removes the worker's window, worktree and local branch, which frees its slot, and notifies the user to run `/afk next` for the next batch. A merged ticket is marked done. A landed ticket is completed: a local ticket is marked done, and a GitHub issue is closed as completed with the comment `Landed in <sha> on <target>` (on an integration branch too: closed means done on the target, not on base). A ticket whose PR was closed unmerged is released back onto the frontier (on GitHub, unassigned). A closed ticket is left as it is, as is any PR still open on its branch. It does this only when the worker's shell pane is idle at a shell prompt (or gone, as when its window was closed), the worktree has no uncommitted changes and, unless the PR merged or the branch landed, the branch has no commits that were never pushed (a landed branch's work is in the target, and a local-mode branch is never pushed). Otherwise the worker becomes `cleanup-pending` and the user is notified once. The watcher checks again every tick and cleans up as soon as it's safe. Tell the user what is holding cleanup up. Don't clean up by hand.

## Worker protocol

Workers end every phase with `afk report <done|blocked|question> "<message>"`. That, plus the hooks, is what `afk status` shows.

- `question` or `blocked`: the watcher notifies and does not advance. Tell the user which window needs them — do not answer on the worker's behalf.
- A worker that stops without reporting since its last phase prompt stays `working`, since workers end turns to wait on background tasks (a review subagent, CI checks, reviewer bots) and resume when they land. If its session stays quiet past `idle_minutes`, it is marked `attention` (window suffix `!`): it has stalled silently and needs the user.
- A worker due its next phase whose pane no longer runs `claude` (it exited, or the pane was killed) is also marked `attention`; the watcher never pastes a prompt into a bare shell.
- Once the user types into a worker that is `question`, `blocked`, `stuck` or silently stalled, it is `working` again and its phase clock restarts, so time spent waiting on the user never counts against its limits.

## Limits

The watcher contains runaway workers. Defaults, overridable in the project's `config.toml` under `[limits]`:

```toml
[limits]
max_workers = 3     # `afk start` refuses beyond this; workers in review and hitl workers don't count
phase_minutes = 90  # wall-clock in one phase while working
idle_minutes = 20   # working, but its transcript hasn't changed
fix_loops = 3       # review rounds (feedback → fix → done) before stopping to look
critique_rounds = 3 # local mode: critique rounds that still found something to fix, before stopping to look
```

A tripped limit interrupts the worker (Escape in its pane; not for `fix_loops` or `critique_rounds`, where it has already stopped, nor for a worker that stopped without reporting: only `idle_minutes` applies to it, and once it trips the worker is marked `attention` instead), marks it `stuck` with the reason (window suffix `!`) and notifies. Nothing is killed: the session stays open for the user or `/afk next` triage. A limit trips at most once per phase clock. A stuck worker that reports `done` moves on as usual. One the user nudges in its pane gets a fresh clock, so a limit can trip again only if it runs away again.

## Setup

Run `/afk-setup` once in each repo first: it writes the repo's `docs/agents/afk.md`, proves it, and on a new machine puts `afk` on PATH for workers and the user.
